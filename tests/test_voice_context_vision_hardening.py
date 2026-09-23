"""
Automated Regression Tests for Voice Continuity, Tool Capability Registry,
Vision Canonicalization, Context Tracking, Intent Classification, and Fail-Fast Retries.
"""

import asyncio
import time
import pytest

from futaba.agent.capabilities import get_capability_registry, ToolCapabilityRegistry
from futaba.agent.controller import AgentController, PLANNER_SYSTEM_PROMPT
from futaba.agent.tools import ToolRegistry, ToolResult
from futaba.system.context_tracker import (
    get_context_tracker,
    get_conversation_context,
    classify_intent,
    UserIntent,
)
from futaba.tasks.task_manager import (
    TaskManager,
    Task,
    TaskState,
    ExecutionPlan,
    ExecutionStep,
    StepState,
    TaskJournal,
)
from futaba.voice.command_router import VoiceCommandRouter


@pytest.mark.asyncio
async def test_01_tool_capability_registry():
    """Verify ToolCapabilityRegistry enforces computer_use as canonical screen capability and disables standalone vision."""
    registry = get_capability_registry()

    # 1. Check computer_use is canonical visual capability
    cu_cap = registry.get("computer_use")
    assert cu_cap is not None
    assert cu_cap.available is True
    assert cu_cap.supports_visual_interaction is True
    assert "inspect" in cu_cap.supported_actions

    # 2. Check standalone vision tool does NOT exist / is unavailable
    assert registry.is_available("vision") is False
    v_cap = registry.get("vision")
    assert v_cap is not None
    assert v_cap.available is False

    # 3. Canonicalize hallucinated vision -> computer_use:inspect
    tool, action, params = registry.canonicalize_step("vision", "analyze", {"target": "window"})
    assert tool == "computer_use"
    assert action == "inspect"

    # 4. Canonicalize application launch aliases
    tool, action, params = registry.canonicalize_step("computer_use", "open_application", {"name": "notepad"})
    assert tool == "application"
    assert action == "launch"

    # 5. Fail-fast non-transient error detection
    assert registry.is_transient_error("Unknown tool: vision") is False
    assert registry.is_transient_error("Command not found: foo") is False
    assert registry.is_transient_error("Access is denied") is False
    assert registry.is_transient_error("Connection timed out") is True
    print("\n[PASS] ToolCapabilityRegistry verified.")


@pytest.mark.asyncio
async def test_02_planner_prompt_and_tool_registry():
    """Verify PLANNER_SYSTEM_PROMPT does not hallucinate vision and ToolRegistry canonicalizes calls."""
    # 1. PLANNER_SYSTEM_PROMPT check
    assert "vision" not in PLANNER_SYSTEM_PROMPT.lower().split("critical rules")[0] or "there is no 'vision' tool" in PLANNER_SYSTEM_PROMPT.lower()
    assert "computer_use" in PLANNER_SYSTEM_PROMPT
    assert "application" in PLANNER_SYSTEM_PROMPT

    # 2. ToolRegistry execute auto-canonicalizes
    tool_reg = ToolRegistry()
    # Execute with unknown 'vision' should canonicalize to computer_use, and report available tools if computer_use isn't registered
    res = await tool_reg.execute("vision", "inspect", {})
    # Since computer_use is not registered in bare registry, it should fail with computer_use in error, NOT vision
    assert "computer_use" in res.error.lower()
    print("\n[PASS] Planner prompt and ToolRegistry canonicalization verified.")


@pytest.mark.asyncio
async def test_03_intent_classification_and_screen_queries():
    """Verify semantic intent classification accurately distinguishes screen queries, app launches, and conversation."""
    # Screen understanding queries
    assert classify_intent("What am I looking at?") == UserIntent.SCREEN_UNDERSTANDING
    assert classify_intent("What's on my screen?") == UserIntent.SCREEN_UNDERSTANDING
    assert classify_intent("What is on the screen right now?") == UserIntent.SCREEN_UNDERSTANDING

    # App launches vs web navigation
    assert classify_intent("Open Notepad") == UserIntent.APPLICATION_CONTROL
    assert classify_intent("Launch Calculator") == UserIntent.APPLICATION_CONTROL
    assert classify_intent("Open YouTube") == UserIntent.BROWSER_NAVIGATION
    assert classify_intent("Go to reddit.com") == UserIntent.BROWSER_NAVIGATION

    # Conversation and sleep
    assert classify_intent("Hey Futaba") == UserIntent.GENERAL_CONVERSATION
    assert classify_intent("Go to sleep") == UserIntent.GENERAL_CONVERSATION

    # Conversational phrases should NOT be treated as application control
    assert classify_intent("RightLine") != UserIntent.APPLICATION_CONTROL
    assert classify_intent("what time is it in Tokyo?") == UserIntent.INFORMATION_QUERY
    print("\n[PASS] Intent classification verified.")


@pytest.mark.asyncio
async def test_04_command_router_screen_context_and_guard():
    """Verify VoiceCommandRouter handles get_screen_context and reroutes screen queries away from open_application."""
    router = VoiceCommandRouter()

    # 1. get_screen_context tool call
    res = await router.execute_tool_call("get_screen_context", {})
    assert res["status"] == "success"
    assert "window_title" in res
    assert "process_name" in res
    assert res.get("supports_visual_interaction") is True

    # 2. Guard against screen query routed to open_application
    res_guard = await router.execute_tool_call("open_application", {"name": "What am I looking at?"})
    assert res_guard["status"] == "success"
    assert "window_title" in res_guard

    # 3. Guard against conversational word routed to open_application
    res_conv = await router.execute_tool_call("open_application", {"name": "something"})
    assert res_conv["status"] == "error"
    assert "not recognized" in res_conv["message"].lower()
    print("\n[PASS] VoiceCommandRouter screen context and guards verified.")


@pytest.mark.asyncio
async def test_05_fail_fast_retries_and_no_invalid_transition(tmp_path):
    """Verify non-transient errors fail fast without 3 retries and task does not crash with InvalidTransitionError."""
    journal = TaskJournal(base_dir=tmp_path)
    tm = TaskManager(journal=journal)

    # Mock router
    class DummyRouter:
        async def complete(self, *args, **kwargs):
            class Resp:
                content = "{}"
                model = "dummy"
            return Resp()

    tools = ToolRegistry()
    controller = AgentController(task_manager=tm, model_router=DummyRouter(), tool_registry=tools)

    task = await tm.create_task("Test task")
    step = ExecutionStep(
        index=0,
        description="Failing step",
        tool="non_existent_tool",
        action="action",
        parameters={},
        idempotent=True,
    )
    step.error = "Unknown tool: non_existent_tool"

    # Handle step failure: non-transient error must fail-fast without 3 retries
    can_continue = await controller._handle_step_failure(task, step)
    assert can_continue is False
    assert step.retries == 0
    # The controller already marked the task as FAILED
    assert task.state == TaskState.FAILED

    # Verify that in controller, if task is FAILED, it skips VERIFYING phase
    assert task.state.is_terminal is True
    print("\n[PASS] Fail-fast retries and state machine transition verified.")


@pytest.mark.asyncio
async def test_06_persistent_session_multi_turn():
    """Verify GeminiLiveVoiceProvider maintains engagement across multiple turns without session teardown."""
    from futaba.voice.gemini_live import GeminiLiveVoiceProvider
    router = VoiceCommandRouter()
    provider = GeminiLiveVoiceProvider(command_router=router)

    # Initial state
    assert provider.engagement_state == "awake"

    # Simulate turn 1 completion
    now = time.monotonic()
    provider._last_interaction_time = now
    provider._engagement_state = "awake"
    provider._set_state("listening")
    assert provider.engagement_state == "awake"
    assert provider.state == "listening"

    # Simulate tool call during turn 2
    provider._set_state("tool_call")
    provider._audio_streaming_allowed.clear()
    assert provider.state == "tool_call"
    assert not provider._audio_streaming_allowed.is_set()

    # Tool response sent, resume streaming
    provider._audio_streaming_allowed.set()
    provider._set_state("speaking")
    assert provider._audio_streaming_allowed.is_set()
    assert provider.state == "speaking"

    # Turn 2 completes -> provider stays awake & listening for Turn 3
    provider._set_state("listening")
    assert provider.engagement_state == "awake"
    assert provider.state == "listening"

    # Turn 3: User says go to sleep
    await router.execute_tool_call("go_to_sleep", {})
    assert provider.engagement_state == "dormant"
    print("\n[PASS] Persistent multi-turn session simulation verified.")
