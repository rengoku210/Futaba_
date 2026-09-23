"""
Autonomous End-to-End Task Execution Test for FUTABA.

Tests the complete lifecycle:
1. Task Creation (QUEUED)
2. Planning (PLANNING -> ExecutionPlan with steps)
3. Step-by-Step Execution (RUNNING):
   - Launch application (Notepad)
   - Background inspection & typing via computer_use (Hermes CUA driver / native fallback)
   - Save text content to a temporary verification file
   - Verify file contents match expected output
4. Verification & State Machine Transition (VERIFYING -> COMPLETED)
5. Crash Recovery Journaling verification
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Add source and hermes paths
sys.path.insert(0, "src")
sys.path.insert(0, "hermes")

from futaba.core.config import FutabaConfig
from futaba.tasks.task_manager import TaskManager, TaskJournal, TaskState, ExecutionPlan, ExecutionStep, StepState
from futaba.agent.tools import ToolRegistry
from futaba.agent.hermes_bridge import HermesBridge
from futaba.agent.hermes_tools import HermesComputerUseTool, HermesExecutionTool, HermesBrowserTool
from futaba.agent.tools import TerminalTool, FilesystemTool, ApplicationTool
from futaba.agent.controller import AgentController
from futaba.routing.model_router import create_router


async def run_e2e_test():
    print("=" * 60)
    print("FUTABA AUTONOMOUS E2E TASK EXECUTION TEST")
    print("=" * 60)

    # 1. Initialize Components
    print("\n[Step 1] Initializing Futaba subsystems...")
    bridge = HermesBridge()
    await bridge.initialize()
    print(f"       Hermes Bridge initialized: {bridge.is_available}")

    registry = ToolRegistry()
    registry.register(TerminalTool())
    registry.register(FilesystemTool())
    registry.register(ApplicationTool())
    registry.register(HermesComputerUseTool(bridge))
    registry.register(HermesBrowserTool(bridge))
    print(f"       Tool Registry loaded {len(registry.tools)} tools: {[t.name for t in registry.tools]}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        journal_dir = Path(tmp_dir) / "tasks"
        journal = TaskJournal(base_dir=journal_dir)
        task_manager = TaskManager(journal=journal)
        router = create_router()

        controller = AgentController(
            task_manager=task_manager,
            model_router=router,
            tool_registry=registry,
        )

        # 2. Create Task
        print("\n[Step 2] Creating autonomous task...")
        objective = "Open Notepad, type 'Futaba is operational', save to temp file, verify file exists."
        task = await task_manager.create_task(
            user_request=objective,
            title="E2E Notepad Automation & Verification",
        )
        assert task.state == TaskState.QUEUED
        print(f"       Task created: {task.task_id[:8]} in state {task.state.value}")

        # 3. Transition to PLANNING and assign concrete execution plan
        print("\n[Step 3] Formulating execution plan...")
        await task_manager.transition(task.task_id, TaskState.PLANNING, "Generating plan steps")

        test_file_path = str(Path(tmp_dir) / "futaba_e2e_output.txt")
        test_content = "Futaba autonomous desktop copilot is operational."

        steps = [
            ExecutionStep(
                index=0,
                description="Launch Notepad application",
                tool="application",
                action="launch",
                parameters={"app_name": "notepad.exe"},
            ),
            ExecutionStep(
                index=1,
                description="Inspect Notepad window via computer_use",
                tool="computer_use",
                action="capture",
                parameters={"app": "notepad.exe", "window_title": "Notepad", "mode": "ax"},
            ),
            ExecutionStep(
                index=2,
                description="Type status text into Notepad via computer_use",
                tool="computer_use",
                action="type",
                parameters={"text": test_content, "app": "notepad.exe", "window_title": "Notepad"},
            ),
            ExecutionStep(
                index=3,
                description="Save verified output to disk via filesystem tool",
                tool="filesystem",
                action="write_file",
                parameters={"path": test_file_path, "content": test_content},
            ),
            ExecutionStep(
                index=4,
                description="Verify written output exists and matches expected content",
                tool="filesystem",
                action="read_file",
                parameters={"path": test_file_path},
            ),
            ExecutionStep(
                index=5,
                description="Close Notepad process",
                tool="application",
                action="kill",
                parameters={"app_name": "notepad.exe"},
            ),
        ]

        plan = ExecutionPlan(objective=objective, steps=steps)
        await task_manager.update_plan(task.task_id, plan)
        print(f"       Plan created with {len(steps)} steps.")

        # 4. Transition to RUNNING and Execute Steps
        print("\n[Step 4] Executing plan steps...")
        await task_manager.transition(task.task_id, TaskState.RUNNING, "Starting execution")

        for step in plan.steps:
            print(f"       Executing Step {step.index}: {step.description}...")
            step.state = StepState.RUNNING
            step_start = time.monotonic()

            result = await registry.execute(
                tool_name=step.tool,
                action=step.action,
                parameters=step.parameters,
            )

            step.duration_seconds = time.monotonic() - step_start
            if result.success:
                step.state = StepState.COMPLETED
                step.result = result.output
                print(f"         -> Step {step.index} COMPLETED ({step.duration_seconds:.2f}s). Result snippet: {str(result.output)[:90]}")
            else:
                step.state = StepState.FAILED
                step.error = result.error
                print(f"         -> Step {step.index} FAILED: {result.error}")
                assert False, f"Step {step.index} failed: {result.error}"

            # Checkpoint journal
            await task_manager.checkpoint(task.task_id, step.index)

        # 5. Verification Phase
        print("\n[Step 5] Transitioning to VERIFYING...")
        await task_manager.transition(task.task_id, TaskState.VERIFYING, "Verifying artifacts")

        assert os.path.exists(test_file_path), "Verification file was not created on disk!"
        with open(test_file_path, "r", encoding="utf-8") as f:
            actual_content = f.read()
        assert actual_content == test_content, f"Content mismatch: expected {test_content}, got {actual_content}"
        print(f"       PASS: Verified file exists with correct content: '{actual_content}'")

        # 6. Complete Task
        print("\n[Step 6] Completing task...")
        await task_manager.transition(task.task_id, TaskState.COMPLETED, "All steps verified successfully")
        assert task.state == TaskState.COMPLETED
        print(f"       Task {task.task_id[:8]} transitioned to {task.state.value}")

        # 7. Check Crash Recovery / Journal Integrity
        print("\n[Step 7] Testing journal integrity from disk...")
        recovered_task = journal.load(task.task_id)
        assert recovered_task is not None
        assert recovered_task.state == TaskState.COMPLETED
        assert recovered_task.plan is not None
        assert len(recovered_task.plan.steps) == 6
        assert all(s.state == StepState.COMPLETED for s in recovered_task.plan.steps)
        print(f"       PASS: Recovered task {recovered_task.task_id[:8]} from journal with all 6 steps completed.")

    print("\n" + "=" * 60)
    print("ALL E2E CHECKS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
