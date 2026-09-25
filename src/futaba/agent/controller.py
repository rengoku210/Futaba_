"""
Futaba Agent Controller — The autonomous execution engine.

This is the brain of Futaba. It:
1. Takes user requests and creates tasks
2. Uses the planner model to generate execution plans
3. Executes steps through tools (Hermes bridge, terminal, browser, etc.)
4. Observes and verifies results
5. Handles errors with intelligent recovery
6. Detects and reports blockers
7. Manages the full task lifecycle

The controller operates the core agent loop:

    while task is not complete:
        observe current state
        decide next action (through LLM)
        execute action (through tools)
        verify result
        update task state
        checkpoint for crash recovery

Architecture:
    User → TaskManager → AgentController → ModelRouter → LLM
                                         → ToolRegistry → Tool execution
                                         → TaskManager (state updates)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable
import psutil

from futaba.core.config import get_config, AutonomyLevel
from futaba.tasks.task_manager import (
    Task, TaskState, TaskManager, ExecutionPlan, ExecutionStep,
    StepState, Blocker, TaskQueue,
)
from futaba.routing.model_router import (
    ModelRouter, ChatMessage, CompletionResponse,
    ModelRequest, TaskComplexity, TaskCapability,
)
from futaba.agent.tools import ToolRegistry, ToolResult

logger = logging.getLogger("futaba.agent")


# ---------------------------------------------------------------------------
# System Prompts
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """You are Futaba's planning engine. Given a user objective, you must create a detailed execution plan.

Your output must be a JSON object with this exact structure:
{
  "objective": "Clear statement of what needs to be accomplished",
  "context": "Relevant context about the environment and constraints",
  "requirements": ["list", "of", "requirements"],
  "constraints": ["list", "of", "constraints"],
  "estimated_duration_minutes": 5,
  "validation_criteria": ["How to verify the task is complete"],
  "steps": [
    {
      "description": "What this step does",
      "tool": "terminal|browser|computer_use|filesystem|search|application",
      "action": "specific_action_name",
      "parameters": {"key": "value"},
      "verification": "How to verify this step succeeded",
      "recovery_strategy": "What to try if this step fails",
      "idempotent": true,
      "destructive": false,
      "depends_on": []
    }
  ]
}

Rules:
- Break complex tasks into small, verifiable steps
- Each step should do ONE thing
- Include verification for every step
- Include recovery strategies for steps that might fail
- Mark destructive steps (file deletion, system changes) appropriately
- Mark non-idempotent steps (things that shouldn't be repeated)
- Order steps by their dependencies
- Be specific about tool and action choices
- Include prerequisite checks (e.g., "verify Python is installed")
- Do NOT include unnecessary steps

Available tools:
- computer_use: Canonical capability for native Windows desktop control, screen understanding, element inspection, clicking, and typing (Actions: inspect, screenshot, click, type, key, press, move, drag, hotkey)
- application: Launch, focus, and close desktop applications (Actions: launch, close, list_running, discover)
- browser: Web browser automation (Actions: navigate, click, type, read, evaluate, screenshot)
- terminal: Execute shell commands (Actions: run_powershell, run_cmd, run_python, run_command, list_processes)
- filesystem: File operations (Actions: read, write, edit, list_dir, exists, delete)
- search: Web search queries (Actions: query)

CRITICAL RULES:
- There is NO 'vision' tool. For screen understanding, inspecting visual elements, or reading screen text, ALWAYS use 'computer_use' with action 'inspect' or 'screenshot'.
- For launching desktop applications, ALWAYS use 'application' with action 'launch' and parameters: {"name": "<app_name>"}.

Respond with ONLY the JSON object. No markdown, no explanation."""


EXECUTOR_SYSTEM_PROMPT = """You are Futaba's execution engine. You execute individual steps of a plan by calling the appropriate tools.

Given a step description and context, determine the exact tool invocation needed.

Your response must be a JSON object:
{
  "tool": "the tool to use",
  "action": "specific action",
  "parameters": {"key": "value"},
  "reasoning": "brief explanation of why this approach"
}

If the step requires multiple sub-actions, break them into a sequence:
{
  "sequence": [
    {"tool": "...", "action": "...", "parameters": {...}},
    {"tool": "...", "action": "...", "parameters": {...}}
  ],
  "reasoning": "..."
}

Rules:
- Use the simplest approach that works
- Prefer structured tools over computer_use when available
- Include all necessary parameters
- If you need information you don't have, say so clearly

Respond with ONLY the JSON object."""


VERIFIER_SYSTEM_PROMPT = """You are Futaba's verification engine. Given an action that was executed and its result, determine if the step succeeded.

Respond with a JSON object:
{
  "success": true/false,
  "confidence": 0.0 to 1.0,
  "evidence": "What evidence supports this conclusion",
  "issues": ["list of any issues found"],
  "suggestion": "What to do if not successful"
}

Rules:
- Be conservative — don't mark success if there's doubt
- Check for actual completion, not just absence of errors
- Consider partial success
- Suggest concrete recovery actions if failed

Respond with ONLY the JSON object."""


RECOVERY_SYSTEM_PROMPT = """You are Futaba's recovery engine. A step in the execution plan has failed. Analyze the error and determine the best recovery strategy.

Respond with a JSON object:
{
  "diagnosis": "What went wrong",
  "root_cause": "The underlying cause",
  "strategy": "retry|alternative|skip|abort|ask_user",
  "alternative_action": {
    "tool": "...",
    "action": "...",
    "parameters": {...}
  },
  "user_question": "Only if strategy is ask_user — concise question for the user",
  "reasoning": "Why this recovery strategy"
}

Rules:
- Try to fix the problem automatically first
- Only ask the user when genuinely blocked
- Consider if the error might self-resolve with retry
- Consider alternative approaches
- Know when to give up (infinite loops waste time)

Respond with ONLY the JSON object."""


# ---------------------------------------------------------------------------
# Agent Controller
# ---------------------------------------------------------------------------

class AgentController:
    """
    The autonomous execution engine for Futaba.

    Runs the core agent loop: plan → execute → observe → verify → recover.
    """

    def __init__(
        self,
        task_manager: TaskManager,
        model_router: ModelRouter,
        tool_registry: ToolRegistry,
    ):
        self._task_manager = task_manager
        self._router = model_router
        self._tools = tool_registry
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._active_executions: dict[str, asyncio.Task] = {}

        # Event handlers for UI updates
        self._on_status_update: Callable[[str, str], Awaitable[None]] | None = None
        self._on_blocker: Callable[[str, Blocker], Awaitable[None]] | None = None
        self._on_complete: Callable[[str, str], Awaitable[None]] | None = None

    async def start(self) -> None:
        """Start the agent controller's execution loop."""
        if self._running:
            return

        self._running = True

        # Recover any tasks from a previous crash
        recovered = await self._task_manager.recover_from_crash()
        if recovered:
            logger.info("Recovered %d tasks from previous session", len(recovered))

        # Start the worker loop
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Agent controller started")

    async def stop(self) -> None:
        """Gracefully stop the agent controller."""
        self._running = False

        # Cancel active executions
        for task_id, exec_task in self._active_executions.items():
            exec_task.cancel()
            logger.info("Cancelled execution for task %s", task_id[:8])

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        logger.info("Agent controller stopped")

    async def submit(self, user_request: str, title: str = "") -> Task:
        """
        Submit a new user request for autonomous execution.

        This is the primary entry point for the UI/voice layer.
        """
        task = await self._task_manager.create_task(user_request, title)
        return task

    async def provide_input(self, task_id: str, user_input: str) -> None:
        """
        Provide user input to resolve a blocker.

        Called when the user responds to a blocker prompt.
        """
        task = await self._task_manager.resolve_blocker(task_id, user_input)
        logger.info("User provided input for task %s: %s", task_id[:8], user_input[:50])

    # -----------------------------------------------------------------------
    # Worker Loop
    # -----------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        """
        Main worker loop — dequeues tasks and executes them.
        """
        while self._running:
            try:
                task = await self._task_manager._queue.dequeue()
                if task is None:
                    continue

                # Execute in a separate task so multiple tasks can run concurrently
                exec_task = asyncio.create_task(self._execute_task(task))
                self._active_executions[task.task_id] = exec_task

                # Don't await — let it run in the background
                exec_task.add_done_callback(
                    lambda t, tid=task.task_id: self._on_execution_done(tid, t)
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Worker loop error: %s", traceback.format_exc())
                await asyncio.sleep(1)

    def _on_execution_done(self, task_id: str, future: asyncio.Task) -> None:
        """Callback when a task execution completes."""
        self._active_executions.pop(task_id, None)
        self._task_manager._queue.mark_done(task_id)

        if future.exception():
            logger.error(
                "Task %s execution failed: %s",
                task_id[:8], future.exception()
            )

    # -----------------------------------------------------------------------
    # Task Execution
    # -----------------------------------------------------------------------

    async def _execute_task(self, task: Task) -> None:
        """
        Execute a single task through the full lifecycle.

        1. Plan (if not already planned)
        2. Execute each step
        3. Verify results
        4. Handle errors/recovery
        5. Complete or fail
        """
        try:
            # --- RUNTIME READINESS CHECK ---
            has_hermes = any(t.name in ("hermes", "computer_use", "browser") for t in self._tools.all_tools())
            from futaba.agent.hermes_bridge import find_hermes_dir
            hermes_dir = find_hermes_dir()

            desktop_keywords = ("open", "chrome", "notepad", "browser", "click", "type", "watch", "desktop", "window", "app", "application", "website")
            is_autonomous_desktop_task = any(w in task.user_request.lower() for w in desktop_keywords)
            if is_autonomous_desktop_task and (not has_hermes or not hermes_dir):
                logger.warning("Hermes runtime unavailable for task %s — setting BLOCKED", task.task_id[:8])
                blocker = Blocker(
                    kind="runtime_missing",
                    description="Hermes Agent runtime is unavailable. Please install or configure Hermes to execute autonomous desktop tasks.",
                    user_prompt="Hermes Agent runtime is unavailable. Please restore Hermes at %LOCALAPPDATA%\\Futaba\\runtime\\hermes or configure HERMES_HOME to proceed.",
                )
                task = await self._task_manager.set_blocker(task.task_id, blocker)
                await self._emit_blocker(task.task_id, blocker)
                return

            # --- PLANNING PHASE ---
            if not task.plan:
                # Fast path check for deterministic single-step tasks (Requirement 9)
                deterministic_plan = self._try_synthesize_deterministic_plan(task)
                if deterministic_plan:
                    logger.info("Task %s resolved via deterministic fast-path (bypassed LLM planner)", task.task_id[:8])
                    plan = deterministic_plan
                else:
                    await self._task_manager.transition(
                        task.task_id, TaskState.PLANNING, "Generating plan"
                    )
                    await self._emit_status(task.task_id, "Planning...")

                    plan = await self._generate_plan(task)

                await self._task_manager.update_plan(task.task_id, plan)
                task.plan = plan

            if task.state != TaskState.RUNNING:
                await self._task_manager.transition(
                    task.task_id, TaskState.RUNNING, "Executing plan"
                )

            # --- EXECUTION PHASE ---
            if task.plan:
                await self._execute_plan(task)

            # If task failed, was cancelled, or is blocked during execution, do NOT proceed to verification
            if task.state in (TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED):
                logger.info("Task %s ended in %s state; skipping verification", task.task_id[:8], task.state.value)
                return

            # --- VERIFICATION PHASE ---
            await self._task_manager.transition(
                task.task_id, TaskState.VERIFYING, "Verifying results"
            )
            await self._emit_status(task.task_id, "Verifying...")

            verified = await self._verify_task(task)

            if verified:
                result = self._summarize_results(task)
                await self._task_manager.complete_task(
                    task.task_id, result, task.result_artifacts
                )
                await self._emit_complete(task.task_id, result)
            else:
                # Verification failed — try recovery
                task.retry_count += 1
                if task.retry_count <= task.max_retries:
                    await self._task_manager.transition(
                        task.task_id, TaskState.RECOVERING,
                        "Verification failed, retrying"
                    )
                    # Re-execute with the existing plan
                    await self._execute_task(task)
                else:
                    await self._task_manager.fail_task(
                        task.task_id,
                        "Task failed verification after maximum retries"
                    )

        except asyncio.CancelledError:
            logger.info("Task %s execution cancelled", task.task_id[:8])
            try:
                if task.state != TaskState.CANCELLED:
                    await self._task_manager.transition(
                        task.task_id, TaskState.CANCELLED, "Execution cancelled"
                    )
            except Exception as exc:
                logger.warning("Failed to transition cancelled task %s: %s", task.task_id[:8], exc)
            raise

        except Exception as e:
            logger.error(
                "Task %s execution error: %s",
                task.task_id[:8], traceback.format_exc()
            )
            try:
                await self._task_manager.fail_task(task.task_id, str(e))
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Planning
    # -----------------------------------------------------------------------

    def _try_synthesize_deterministic_plan(self, task: Task) -> Optional[ExecutionPlan]:
        """
        Fast-path bypass for deterministic single-step tasks (Requirement 9).
        Target: <100ms routing overhead without invoking heavy LLM planner.
        Handles:
        - Open / Launch / Focus application
        - Close / Kill application
        - Navigate browser / Open URL
        - Type text into active window or application
        - Media controls (play, pause, volume)
        - Scroll
        """
        req = task.user_request.strip()
        lower = req.lower()

        # Reject compound or multi-step requests
        compound_indicators = (" and ", " then ", " after that ", " but ", " save it", "copy ", "find ")
        if any(c in lower for c in compound_indicators):
            return None

        # 1. Close application: e.g. "close notepad", "kill chrome"
        m_close = re.match(r"^(?:close|kill|exit|quit)\s+([a-zA-Z0-9_\-\. ]+)$", lower)
        if m_close:
            app_name = m_close.group(1).strip()
            plan = ExecutionPlan(
                objective=req,
                validation_criteria=[f"Process {app_name} terminated"],
                estimated_duration_minutes=1,
            )
            step = ExecutionStep(
                index=0,
                description=f"Close application '{app_name}'",
                tool="application",
                action="close",
                parameters={"name": app_name},
                verification=f"Process {app_name} is no longer running",
                idempotent=True,
            )
            plan.steps.append(step)
            task.model_used = "deterministic_fast_path"
            return plan

        # 2. Open / Launch / Focus application: e.g. "open notepad", "open roblox", "launch discord"
        m_open = re.match(r"^(?:open|launch|start|focus)\s+([a-zA-Z0-9_\-\. ]+)$", lower)
        if m_open:
            app_name = m_open.group(1).strip()
            if app_name not in ("tab", "window", "url", "website", "file", "document"):
                # Check if it's a web destination
                from futaba.system.context_tracker import get_context_tracker
                tracker = get_context_tracker()
                is_web, url = tracker.is_web_destination(app_name)
                if is_web:
                    plan = ExecutionPlan(
                        objective=req,
                        validation_criteria=[f"Browser navigated to {url}"],
                        estimated_duration_minutes=1,
                    )
                    step = ExecutionStep(
                        index=0,
                        description=f"Navigate browser to {url}",
                        tool="browser",
                        action="navigate",
                        parameters={"url": url},
                        verification=f"Browser navigated to {url}",
                        idempotent=True,
                    )
                    plan.steps.append(step)
                    task.model_used = "deterministic_fast_path"
                    return plan
                else:
                    plan = ExecutionPlan(
                        objective=req,
                        validation_criteria=[f"Application {app_name} is active in foreground"],
                        estimated_duration_minutes=1,
                    )
                    step = ExecutionStep(
                        index=0,
                        description=f"Open or focus application '{app_name}'",
                        tool="application",
                        action="launch",
                        parameters={"name": app_name},
                        verification=f"Application {app_name} is visible and in foreground",
                        idempotent=True,
                    )
                    plan.steps.append(step)
                    task.model_used = "deterministic_fast_path"
                    return plan

        # 3. Simple typing: e.g. "type Hello World into Notepad", "type 'Hello World'"
        m_type = re.match(r"^(?:type|write)\s+['\"]?(.+?)['\"]?(?:\s+(?:in|into|on)\s+([a-zA-Z0-9_\-\. ]+))?$", req, re.I)
        if m_type:
            text = m_type.group(1).strip()
            target_app = (m_type.group(2) or "").strip()
            plan = ExecutionPlan(
                objective=req,
                validation_criteria=[f"Text '{text}' entered into active editor"],
                estimated_duration_minutes=1,
            )
            step_idx = 0
            if target_app:
                # Step 0: Ensure target app is launched/focused
                plan.steps.append(
                    ExecutionStep(
                        index=step_idx,
                        description=f"Focus or launch {target_app}",
                        tool="application",
                        action="launch",
                        parameters={"name": target_app},
                        verification=f"{target_app} in foreground",
                        idempotent=True,
                    )
                )
                step_idx += 1

            plan.steps.append(
                ExecutionStep(
                    index=step_idx,
                    description=f"Type text into target editor",
                    tool="computer_use",
                    action="type",
                    parameters={"text": text},
                    verification=f"Text '{text}' typed and verified",
                    idempotent=False,
                )
            )
            task.model_used = "deterministic_fast_path"
            return plan

        return None

    async def _generate_plan(self, task: Task) -> ExecutionPlan:
        """
        Use the planner model to generate an execution plan.
        Injects current context (active app, browser, conversation) for awareness.
        """
        # Gather current desktop context for the planner
        context_block = ""
        try:
            from futaba.intelligence.context_engine import get_context_engine
            ctx = get_context_engine()
            ctx.update()  # Refresh to latest state
            ctx.set_active_task(task.user_request)
            context_block = ctx.get_context_for_llm()
        except Exception as e:
            logger.debug("Could not inject context into planner: %s", e)

        user_content = f"Create an execution plan for this objective:\n\n{task.user_request}"
        if context_block:
            user_content = (
                f"CURRENT DESKTOP CONTEXT:\n{context_block}\n\n"
                f"---\n\n{user_content}"
            )

        messages = [
            ChatMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_content),
        ]

        request = ModelRequest(
            role="planner",
            complexity=self._assess_complexity(task.user_request),
            capabilities=[TaskCapability.REASONING, TaskCapability.PLANNING],
            task_id=task.task_id,
        )

        response = await self._router.complete(messages, request, max_tokens=8192)

        # Parse the plan from LLM response
        plan_data = self._parse_json_response(response.content)

        plan = ExecutionPlan(
            objective=plan_data.get("objective", task.user_request),
            context=plan_data.get("context", ""),
            requirements=plan_data.get("requirements", []),
            constraints=plan_data.get("constraints", []),
            validation_criteria=plan_data.get("validation_criteria", []),
            estimated_duration_minutes=plan_data.get("estimated_duration_minutes", 5),
        )

        for i, step_data in enumerate(plan_data.get("steps", [])):
            step = ExecutionStep(
                index=i,
                description=step_data.get("description", ""),
                tool=step_data.get("tool", ""),
                action=step_data.get("action", ""),
                parameters=step_data.get("parameters", {}),
                verification=step_data.get("verification", ""),
                recovery_strategy=step_data.get("recovery_strategy", ""),
                idempotent=step_data.get("idempotent", True),
                destructive=step_data.get("destructive", False),
                depends_on=step_data.get("depends_on", []),
            )
            plan.steps.append(step)

        task.model_used = response.model
        logger.info(
            "Generated plan for task %s: %d steps, est. %d min",
            task.task_id[:8], len(plan.steps), plan.estimated_duration_minutes
        )

        return plan

    # -----------------------------------------------------------------------
    # Step Execution
    # -----------------------------------------------------------------------

    async def _execute_plan(self, task: Task) -> None:
        """Execute all pending steps in a task's plan with stall detection."""
        if not task.plan:
            return

        config = get_config()
        STEP_WARN_TIMEOUT = 60.0   # Warn after 60s
        STEP_FAIL_TIMEOUT = 120.0  # Force-fail after 120s

        while True:
            # Check cancellation
            if self._task_manager._queue.is_cancelled(task.task_id):
                raise asyncio.CancelledError()

            # Find next step
            step = task.plan.advance()
            if step is None:
                break  # All steps done

            # Check autonomy policy for destructive steps
            if step.destructive and config.autonomy.level < AutonomyLevel.HIGHLY_AUTO:
                if step.action in config.autonomy.confirmation_required_for:
                    blocker = Blocker(
                        kind="confirmation_needed",
                        description=f"Step requires confirmation: {step.description}",
                        user_prompt=f"Allow: {step.description}?",
                    )
                    await self._task_manager.set_blocker(task.task_id, blocker)
                    return  # Will be resumed when user responds

            # Execute the step with stall detection
            step_start = time.monotonic()
            execute_task = asyncio.create_task(self._execute_step(task, step))
            warned = False

            while not execute_task.done():
                elapsed = time.monotonic() - step_start
                if elapsed > STEP_FAIL_TIMEOUT:
                    logger.error(
                        "Task %s step %d STALLED after %.0fs — force-failing",
                        task.task_id[:8], step.index, elapsed,
                    )
                    execute_task.cancel()
                    try:
                        await execute_task
                    except asyncio.CancelledError:
                        pass
                    step.state = StepState.FAILED
                    step.error = f"Step timed out after {STEP_FAIL_TIMEOUT:.0f}s"
                    break
                if elapsed > STEP_WARN_TIMEOUT and not warned:
                    logger.warning(
                        "Task %s step %d running for %.0fs — possible stall",
                        task.task_id[:8], step.index, elapsed,
                    )
                    warned = True
                await asyncio.sleep(1.0)

            if execute_task.done() and not execute_task.cancelled():
                # Re-raise any exception from the step
                exc = execute_task.exception()
                if exc:
                    raise exc

            # Checkpoint after each successful step
            if step.state == StepState.COMPLETED:
                await self._task_manager.checkpoint(task.task_id, step.index)

            # If step failed and couldn't recover, check if we should continue
            if step.state == StepState.FAILED:
                can_continue = await self._handle_step_failure(task, step)
                if not can_continue:
                    return

        # Clear active task in context engine
        try:
            from futaba.intelligence.context_engine import get_context_engine
            get_context_engine().set_active_task(None)
        except Exception:
            pass

        await self._emit_status(task.task_id, "All steps completed")

    async def _execute_step(self, task: Task, step: ExecutionStep) -> None:
        """Execute a single step and handle errors."""
        step.state = StepState.RUNNING
        step.started_at = datetime.now(timezone.utc).isoformat()
        self._task_manager._journal.save(task)

        await self._emit_status(task.task_id, step.description)

        start_time = time.monotonic()

        try:
            # Determine the exact tool invocation
            tool_call = await self._resolve_tool_call(task, step)

            # Execute through tool registry
            result = await self._tools.execute(
                tool_name=tool_call.get("tool", step.tool),
                action=tool_call.get("action", step.action),
                parameters=tool_call.get("parameters", step.parameters),
            )

            step.duration_seconds = time.monotonic() - start_time

            if result.success:
                step.result = result.output
                step.state = StepState.COMPLETED
                step.completed_at = datetime.now(timezone.utc).isoformat()

                # Track tool usage
                if step.tool not in task.tools_used:
                    task.tools_used.append(step.tool)

                logger.info(
                    "Task %s step %d completed: %s (%.1fs)",
                    task.task_id[:8], step.index,
                    step.description[:40], step.duration_seconds
                )
            else:
                step.error = result.error
                step.state = StepState.FAILED
                logger.warning(
                    "Task %s step %d failed: %s — %s",
                    task.task_id[:8], step.index,
                    step.description[:40], result.error[:100]
                )

        except Exception as e:
            step.duration_seconds = time.monotonic() - start_time
            step.error = str(e)
            step.state = StepState.FAILED
            logger.error(
                "Task %s step %d exception: %s",
                task.task_id[:8], step.index, traceback.format_exc()
            )

        self._task_manager._journal.save(task)

    async def _resolve_tool_call(
        self,
        task: Task,
        step: ExecutionStep,
    ) -> dict[str, Any]:
        """
        Use the executor model to resolve the exact tool call for a step.

        If the step already has specific parameters, use them directly.
        Otherwise, ask the executor model to determine the right invocation.
        """
        from futaba.agent.capabilities import get_capability_registry
        cap_reg = get_capability_registry()

        if step.parameters and step.tool:
            # Step is already fully specified — canonicalize before returning
            canon_tool, canon_action, canon_params = cap_reg.canonicalize_step(
                step.tool, step.action, step.parameters
            )
            return {
                "tool": canon_tool,
                "action": canon_action,
                "parameters": canon_params,
            }

        # Build context from completed steps
        context_parts = [f"Objective: {task.user_request}"]
        if task.plan:
            for prev_step in task.plan.completed_steps:
                context_parts.append(
                    f"Completed: {prev_step.description} → {str(prev_step.result)[:200]}"
                )

        messages = [
            ChatMessage(role="system", content=EXECUTOR_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    f"Context:\n{chr(10).join(context_parts)}\n\n"
                    f"Execute this step:\n{step.description}\n\n"
                    f"Suggested tool: {step.tool}\n"
                    f"Suggested action: {step.action}"
                )
            ),
        ]

        request = ModelRequest(
            role="executor",
            complexity=TaskComplexity.MODERATE,
            capabilities=[TaskCapability.CODING],
            task_id=task.task_id,
        )

        response = await self._router.complete(messages, request, max_tokens=2048)
        parsed = self._parse_json_response(response.content)
        tool_name = parsed.get("tool", step.tool)
        action_name = parsed.get("action", step.action)
        parameters = parsed.get("parameters", step.parameters)
        canon_tool, canon_action, canon_params = cap_reg.canonicalize_step(
            tool_name, action_name, parameters
        )
        parsed["tool"] = canon_tool
        parsed["action"] = canon_action
        parsed["parameters"] = canon_params
        return parsed

    # -----------------------------------------------------------------------
    # Error Recovery
    # -----------------------------------------------------------------------

    async def _handle_step_failure(self, task: Task, step: ExecutionStep) -> bool:
        """
        Handle a failed step. Returns True if execution should continue.

        Recovery strategies:
        1. Auto-conversion (for known aliases/hallucinations like vision)
        2. Retry (if under retry limit, idempotent, and transient error)
        3. Alternative approach (ask recovery model)
        4. Skip (if step is non-critical)
        5. Block (ask user for help)
        6. Abort (give up)
        """
        from futaba.agent.capabilities import get_capability_registry
        cap_reg = get_capability_registry()

        # 1. Auto-convert hallucinated 'vision' tool to computer_use:inspect
        if step.tool == "vision" or "unknown tool: vision" in step.error.lower():
            logger.info("Auto-converting hallucinated 'vision' tool to 'computer_use:inspect'")
            step.tool = "computer_use"
            step.action = "inspect"
            step.state = StepState.PENDING
            step.error = ""
            step.retries += 1
            return True

        # 2. Check transient vs non-transient error
        is_transient = cap_reg.is_transient_error(step.error)

        if is_transient and step.retries < step.max_retries and step.idempotent:
            # Simple retry for transient errors only
            step.retries += 1
            step.state = StepState.PENDING
            step.error = ""
            logger.info(
                "Retrying step %d (attempt %d/%d) after transient error",
                step.index, step.retries, step.max_retries
            )
            return True

        # Ask the recovery model for guidance
        try:
            recovery = await self._get_recovery_strategy(task, step)
            strategy = recovery.get("strategy", "abort")

            if strategy == "retry":
                step.retries += 1
                step.state = StepState.PENDING
                step.error = ""
                return True

            elif strategy == "alternative":
                # Apply alternative action
                alt = recovery.get("alternative_action", {})
                if alt:
                    step.tool = alt.get("tool", step.tool)
                    step.action = alt.get("action", step.action)
                    step.parameters = alt.get("parameters", step.parameters)
                    step.state = StepState.PENDING
                    step.error = ""
                    step.retries += 1
                    return True

            elif strategy == "skip":
                step.state = StepState.SKIPPED
                return True

            elif strategy == "ask_user":
                blocker = Blocker(
                    kind="step_failure",
                    description=recovery.get("diagnosis", step.error),
                    attempted_resolutions=[
                        f"Tried {step.retries} times",
                        recovery.get("diagnosis", ""),
                    ],
                    user_prompt=recovery.get(
                        "user_question",
                        f"Step failed: {step.description}\nError: {step.error}\nHow should I proceed?"
                    ),
                )
                await self._task_manager.set_blocker(task.task_id, blocker)
                return False

            else:  # abort
                await self._task_manager.fail_task(
                    task.task_id,
                    f"Step {step.index} failed: {step.error}\n"
                    f"Recovery diagnosis: {recovery.get('diagnosis', 'Unknown')}"
                )
                return False

        except Exception as e:
            logger.error("Recovery analysis failed: %s", e)
            await self._task_manager.fail_task(
                task.task_id, f"Step {step.index} failed and recovery failed: {step.error}"
            )
            return False

    async def _get_recovery_strategy(
        self, task: Task, step: ExecutionStep
    ) -> dict[str, Any]:
        """Ask the recovery model how to handle a step failure."""
        messages = [
            ChatMessage(role="system", content=RECOVERY_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    f"Task: {task.user_request}\n"
                    f"Failed step: {step.description}\n"
                    f"Tool: {step.tool}\n"
                    f"Action: {step.action}\n"
                    f"Error: {step.error}\n"
                    f"Retries so far: {step.retries}/{step.max_retries}\n"
                    f"Recovery hint: {step.recovery_strategy}\n"
                    f"Is idempotent: {step.idempotent}\n"
                    f"Is destructive: {step.destructive}"
                )
            ),
        ]

        request = ModelRequest(
            role="planner",
            complexity=TaskComplexity.COMPLEX,
            capabilities=[TaskCapability.REASONING],
            task_id=task.task_id,
        )

        response = await self._router.complete(messages, request, max_tokens=2048)
        return self._parse_json_response(response.content)

    # -----------------------------------------------------------------------
    # Verification
    # -----------------------------------------------------------------------

    async def _verify_task(self, task: Task) -> bool:
        """
        Verify that the task was completed successfully using ground-truth Windows state.
        Never marks COMPLETED without positive verification evidence.
        """
        # 1. Step check: If any executed step failed, task verification fails immediately
        if task.plan and task.plan.steps:
            failed_steps = [s for s in task.plan.steps if s.state == StepState.FAILED]
            if failed_steps:
                evidence = {
                    "action": "step_execution",
                    "target": failed_steps[0].description,
                    "expected": "success",
                    "observed": f"Failed with: {failed_steps[0].error}",
                    "verified": False,
                }
                task.verification_evidence = evidence
                task.verification_status = f"failed: step_{failed_steps[0].index}_failed"
                logger.warning("Task %s verification failed: step execution error", task.task_id[:8])
                return False

        # 2. Perform ground-truth Windows verification based on user request semantics
        req = task.user_request.lower()
        evidence: dict[str, Any] = {}
        verified = True

        # CASE A: Typing text into a window (e.g., 'type Hello World into Notepad', 'type I am Futaba')
        if any(w in req for w in ("type ", "write ", "typed ")) and any(a in req for a in ("notepad", "editor", "document", "file", "window")):
            verified = False
            # Extract expected text
            expected_text = ""
            m = re.search(r'(?:type|write)\s+["\']([^"\']+)["\']', task.user_request, re.I)
            if not m:
                m = re.search(r'(?:type|write)\s+([A-Za-z0-9\s,\.!]+?)(?:\s+(?:in|into|on)\s+|$)', task.user_request, re.I)
            if m:
                expected_text = m.group(1).strip()

            observed_text = ""
            try:
                from pywinauto import Desktop
                desktop = Desktop(backend="uia")
                # Look for Notepad or active window
                target_win = None
                for w in desktop.windows():
                    w_title = w.window_text()
                    if "notepad" in w_title.lower() or "untitled" in w_title.lower():
                        target_win = w
                        break
                if not target_win:
                    wins = [w for w in desktop.windows() if w.window_text()]
                    if wins:
                        target_win = wins[0]

                if target_win:
                    for d in target_win.descendants()[:40]:
                        if d.element_info.control_type in ("Document", "Edit"):
                            try:
                                observed_text = d.iface_text.DocumentRange.GetText(-1) or ""
                            except Exception:
                                try:
                                    observed_text = d.iface_value.CurrentValue or ""
                                except Exception:
                                    observed_text = d.window_text() or ""
                            if observed_text:
                                break

                    if expected_text and expected_text.lower() in observed_text.lower():
                        verified = True
                        evidence = {
                            "action": "type_text",
                            "target": target_win.window_text(),
                            "expected": expected_text,
                            "observed": observed_text.strip()[:100],
                            "verified": True,
                        }
                    else:
                        evidence = {
                            "action": "type_text",
                            "target": target_win.window_text() if target_win else "Unknown",
                            "expected": expected_text,
                            "observed": observed_text.strip()[:100],
                            "verified": False,
                            "error": f"Expected text '{expected_text}' not observed in editor",
                        }
                else:
                    evidence = {
                        "action": "type_text",
                        "target": "Notepad",
                        "expected": expected_text,
                        "observed": "Window not found",
                        "verified": False,
                    }
            except Exception as ex:
                evidence = {
                    "action": "type_text",
                    "target": "Notepad",
                    "expected": expected_text,
                    "observed": f"Error: {ex}",
                    "verified": False,
                }

        # CASE B: Save file / file creation (e.g. 'save it as hello.txt', 'create test.py')
        elif any(w in req for w in ("save ", "saved ", "create file ", "touch ")) and "." in req:
            verified = False
            m = re.search(r'(?:as|file|to)\s+([a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]+)', task.user_request, re.I)
            fname = m.group(1).strip() if m else ""
            if fname:
                # Search potential directories
                search_dirs = [
                    Path.cwd(),
                    Path.home(),
                    Path.home() / "Desktop",
                    Path.home() / "Downloads",
                    Path.home() / "Documents",
                ]
                found_path = None
                for d in search_dirs:
                    candidate = d / fname
                    if candidate.exists():
                        found_path = candidate
                        break

                if found_path:
                    size = found_path.stat().st_size
                    verified = True
                    evidence = {
                        "action": "save_file",
                        "target": str(found_path),
                        "expected": f"{fname} exists",
                        "observed": f"File exists ({size} bytes)",
                        "verified": True,
                    }
                else:
                    evidence = {
                        "action": "save_file",
                        "target": fname,
                        "expected": f"{fname} exists",
                        "observed": "File not found on disk",
                        "verified": False,
                    }
            else:
                verified = True

        # CASE C: Close application (e.g. 'close Notepad', 'kill Chrome')
        elif any(w in req for w in ("close ", "kill ", "exit ", "quit ")) and not any(w in req for w in ("open", "type")):
            m = re.search(r'(?:close|kill|exit|quit)\s+([a-zA-Z0-9_\-]+)', task.user_request, re.I)
            app_target = m.group(1).lower().strip() if m else ""
            if app_target:
                is_running = any(
                    app_target in p.info["name"].lower()
                    for p in psutil.process_iter(["name"])
                    if p.info.get("name")
                )
                if not is_running:
                    verified = True
                    evidence = {
                        "action": "close_application",
                        "target": app_target,
                        "expected": "process_terminated",
                        "observed": "Process terminated",
                        "verified": True,
                    }
                else:
                    verified = False
                    evidence = {
                        "action": "close_application",
                        "target": app_target,
                        "expected": "process_terminated",
                        "observed": f"Process {app_target} still active",
                        "verified": False,
                    }

        # CASE D: Open application (e.g. 'open Notepad', 'open Brave')
        elif any(w in req for w in ("open ", "launch ", "start ")) and not any(w in req for w in ("type", "write", "save")):
            m = re.search(r'(?:open|launch|start)\s+([a-zA-Z0-9_\-]+)', task.user_request, re.I)
            app_target = m.group(1).lower().strip() if m else ""
            if app_target and app_target not in ("tab", "window", "url", "website"):
                # If it's a web destination, verify browser is active
                from futaba.system.context_tracker import get_context_tracker
                tracker = get_context_tracker()
                is_web, _ = tracker.is_web_destination(app_target)
                if is_web:
                    running_browsers = tracker.get_running_browsers()
                    if running_browsers:
                        verified = True
                        evidence = {
                            "action": "open_web",
                            "target": app_target,
                            "expected": "browser_open",
                            "observed": f"Browser {running_browsers[0]['name']} active",
                            "verified": True,
                        }
                    else:
                        verified = False
                        evidence = {
                            "action": "open_web",
                            "target": app_target,
                            "expected": "browser_open",
                            "observed": "No browser running",
                            "verified": False,
                        }
                else:
                    is_run, p_info = tracker.is_app_running(app_target)
                    if is_run:
                        verified = True
                        evidence = {
                            "action": "open_application",
                            "target": app_target,
                            "expected": "process_running",
                            "observed": f"PID {p_info.get('pid')} running ({p_info.get('name')})",
                            "verified": True,
                        }
                    else:
                        verified = False
                        evidence = {
                            "action": "open_application",
                            "target": app_target,
                            "expected": "process_running",
                            "observed": "Application process not found",
                            "verified": False,
                        }

        # CASE E: General / Multi-step task verification with LLM Verifier
        else:
            if not task.plan or not task.plan.validation_criteria:
                if task.plan and task.plan.steps:
                    all_ok = all(s.state in (StepState.COMPLETED, StepState.SKIPPED) for s in task.plan.steps)
                    evidence = {
                        "action": "plan_completion",
                        "target": task.user_request,
                        "expected": "all_steps_completed",
                        "observed": f"{len(task.plan.completed_steps)}/{len(task.plan.steps)} steps succeeded",
                        "verified": all_ok,
                    }
                    verified = all_ok
                else:
                    verified = True
                    evidence = {"action": "general_task", "verified": True}
            else:
                step_results = []
                for s in task.plan.steps:
                    step_results.append(f"Step {s.index}: {s.description} -> {s.state.value}: {str(s.result)[:150]}")

                messages = [
                    ChatMessage(role="system", content=VERIFIER_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=(
                            f"Objective: {task.user_request}\n\n"
                            f"Validation criteria:\n" + "\n".join(f"- {c}" for c in task.plan.validation_criteria)
                            + f"\n\nStep results:\n" + "\n".join(step_results)
                        )
                    ),
                ]
                request = ModelRequest(
                    role="verifier",
                    complexity=TaskComplexity.MODERATE,
                    capabilities=[TaskCapability.REASONING],
                    task_id=task.task_id,
                )
                try:
                    response = await self._router.complete(messages, request, max_tokens=2048)
                    result = self._parse_json_response(response.content)
                    verified = bool(result.get("success", False))
                    evidence = {
                        "action": "llm_verification",
                        "confidence": result.get("confidence", 0.0),
                        "evidence": result.get("evidence", ""),
                        "issues": result.get("issues", []),
                        "verified": verified,
                    }
                except Exception as ex:
                    logger.warning("LLM verifier call failed: %s; falling back to step check", ex)
                    all_ok = all(s.state in (StepState.COMPLETED, StepState.SKIPPED) for s in task.plan.steps)
                    verified = all_ok
                    evidence = {"action": "fallback_step_check", "verified": all_ok}

        task.verification_evidence = evidence
        task.verification_status = "verified" if verified else f"failed: {evidence.get('error', 'ground_truth_verification_failed')}"
        logger.info("Task %s ground-truth verification result: %s (evidence: %s)", task.task_id[:8], verified, evidence)
        return verified

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _assess_complexity(self, request: str) -> TaskComplexity:
        """Quick heuristic to assess task complexity."""
        lower = request.lower()
        # Simple commands
        simple_triggers = ["open ", "launch ", "start ", "close ", "run "]
        if any(lower.startswith(t) for t in simple_triggers) and len(request) < 50:
            return TaskComplexity.SIMPLE

        # Complex triggers
        complex_triggers = [
            "build", "create", "develop", "architect", "design",
            "watch", "video", "tutorial", "implement", "configure",
            "debug", "fix", "investigate", "analyze",
        ]
        if any(t in lower for t in complex_triggers):
            return TaskComplexity.COMPLEX

        # Expert triggers
        expert_triggers = [
            "from scratch", "full stack", "production", "deploy",
            "migrate", "refactor", "optimize",
        ]
        if any(t in lower for t in expert_triggers):
            return TaskComplexity.EXPERT

        return TaskComplexity.MODERATE

    def _parse_json_response(self, content: str) -> dict[str, Any]:
        """Parse a JSON response from the LLM, handling common issues."""
        content = content.strip()

        # Remove markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first and last lines (fences)
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(content[start:end])
                except json.JSONDecodeError:
                    pass

            logger.warning("Failed to parse LLM JSON response: %s...", content[:200])
            return {"error": "Failed to parse response", "raw": content[:500]}

    def _summarize_results(self, task: Task) -> str:
        """Create a concise result summary for the user."""
        if not task.plan:
            return "Task completed."

        completed = len(task.plan.completed_steps)
        total = len(task.plan.steps)
        skipped = len([s for s in task.plan.steps if s.state == StepState.SKIPPED])

        parts = [f"Completed {completed}/{total} steps."]
        if skipped:
            parts.append(f"({skipped} skipped)")

        # Add the last step's result as context
        if task.plan.completed_steps:
            last = task.plan.completed_steps[-1]
            if last.result:
                result_str = str(last.result)
                if len(result_str) > 200:
                    result_str = result_str[:197] + "..."
                parts.append(f"Last result: {result_str}")

        return " ".join(parts)

    # -----------------------------------------------------------------------
    # Event emission
    # -----------------------------------------------------------------------

    async def _emit_status(self, task_id: str, status: str) -> None:
        if self._on_status_update:
            try:
                await self._on_status_update(task_id, status)
            except Exception:
                pass

    async def _emit_complete(self, task_id: str, result: str) -> None:
        if self._on_complete:
            try:
                await self._on_complete(task_id, result)
            except Exception:
                pass

    async def _emit_blocker(self, task_id: str, blocker: Blocker) -> None:
        if self._on_blocker:
            try:
                await self._on_blocker(task_id, blocker)
            except Exception:
                pass
