"""
Futaba Production Hardening Verification Suite
Tests:
1. Multi-turn context & browser reuse (Brave -> YouTube reuse)
2. Ground-truth Windows verification (Notepad text verification)
3. Continuous conversation engagement state machine (DORMANT -> AWAKE -> SLEEP)
4. Thread safety via SafeQEventLoopPolicy and QtDispatcher
"""

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
import pytest
from futaba.system.context_tracker import get_context_tracker
from futaba.voice.command_router import VoiceCommandRouter
from futaba.voice.gemini_live import GeminiLiveVoiceProvider
from futaba.tasks.task_manager import TaskManager, TaskJournal, Task, TaskState, TaskOrigin, TaskRecoveryPolicy
from futaba.agent.controller import AgentController
from futaba.agent.tools import ToolRegistry, ApplicationTool
from futaba.agent.hermes_tools import HermesComputerUseTool
from futaba.routing.model_router import create_router
from futaba.core.config import get_config, get_quarantine_dir


@pytest.mark.asyncio
async def test_01_context_tracker_browser_reuse():
    """Verify that opening a web destination like YouTube reuses open browser."""
    tracker = get_context_tracker()
    
    # 1. Detect web destination
    is_web, url = tracker.is_web_destination("YouTube")
    assert is_web is True
    assert url == "https://www.youtube.com"

    # 2. open_or_navigate YouTube
    res = tracker.open_or_navigate("YouTube")
    assert res["status"] == "success"
    assert "youtube.com" in res.get("url", "")
    assert res.get("mode") in ("navigated_existing_browser", "launched_browser", "default_browser")
    print(f"\n[PASS] Context tracker open_or_navigate YouTube: mode={res.get('mode')}")


@pytest.mark.asyncio
async def test_02_router_navigate_and_sleep():
    """Verify VoiceCommandRouter handles navigate_browser and go_to_sleep."""
    router = VoiceCommandRouter()
    
    # Check declarations
    tools = router.get_tool_declarations()[0]["function_declarations"]
    tool_names = [t["name"] for t in tools]
    assert "navigate_browser" in tool_names
    assert "go_to_sleep" in tool_names
    
    # Test navigate_browser tool call
    res_nav = await router.execute_tool_call("navigate_browser", {"url": "https://www.google.com"})
    assert res_nav["status"] == "success"
    
    # Test go_to_sleep callback
    slept = False
    def on_sleep():
        nonlocal slept
        slept = True
    router.on_sleep_requested = on_sleep
    
    res_sleep = await router.execute_tool_call("go_to_sleep", {})
    assert res_sleep["status"] == "success"
    assert slept is True
    print("\n[PASS] VoiceCommandRouter navigate_browser and go_to_sleep verified.")


@pytest.mark.asyncio
async def test_03_continuous_engagement_state_machine():
    """Verify engagement states: DORMANT -> AWAKE -> SLEEP & timeout."""
    router = VoiceCommandRouter()
    voice = GeminiLiveVoiceProvider(command_router=router)
    
    assert voice.engagement_state == "awake"
    
    # Go to sleep
    voice.go_to_sleep()
    assert voice.engagement_state == "dormant"
    assert voice.state == "idle"
    
    # Wake up
    voice.wake_up()
    assert voice.engagement_state == "awake"
    assert voice.state == "listening"
    
    # Router sleep triggers voice sleep
    await router.execute_tool_call("go_to_sleep", {})
    assert voice.engagement_state == "dormant"
    print("\n[PASS] Continuous engagement state machine verified.")


@pytest.mark.asyncio
async def test_04_ground_truth_verification():
    """Verify ground truth verification records structured evidence and rejects false success."""
    import subprocess
    from pywinauto import Desktop

    # Launch Notepad
    p = subprocess.Popen(["notepad.exe"])
    time.sleep(1.0)
    try:
        desktop = Desktop(backend="uia")
        target_win = None
        for w in desktop.windows():
            if "notepad" in w.window_text().lower():
                target_win = w
                break
        assert target_win is not None

        # Type text via UIA SetValue
        for d in target_win.descendants()[:30]:
            if d.element_info.control_type in ("Document", "Edit"):
                try:
                    d.iface_value.SetValue("Ground Truth Verification Test")
                    break
                except Exception:
                    pass

        time.sleep(0.3)

        # Create TaskManager with isolated temporary journal so test tasks never pollute production storage
        test_dir = Path(tempfile.mkdtemp(prefix="futaba_test_tasks_"))
        try:
            journal = TaskJournal(base_dir=test_dir)
            tm = TaskManager(journal=journal)
            tools = ToolRegistry()
            router = create_router()
            controller = AgentController(tm, router, tools)

            # 1. Test positive verification: text matches
            task_good = await tm.create_task(
                "Type Ground Truth Verification Test into Notepad",
                origin=TaskOrigin.TEST.value,
            )
            verified_good = await controller._verify_task(task_good)
            assert verified_good is True
            assert task_good.verification_evidence["verified"] is True
            assert "Ground Truth" in task_good.verification_evidence["observed"]
            print(f"\n[PASS] Positive ground-truth verification verified: {task_good.verification_evidence}")

            # 2. Test negative verification: text does NOT match -> must fail!
            task_bad = await tm.create_task(
                "Type Text That Does Not Exist 999 into Notepad",
                origin=TaskOrigin.TEST.value,
            )
            verified_bad = await controller._verify_task(task_bad)
            assert verified_bad is False
            assert task_bad.verification_evidence["verified"] is False
            print(f"\n[PASS] Negative ground-truth verification verified (properly rejected false success): {task_bad.verification_evidence}")
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    finally:
        p.terminate()


@pytest.mark.asyncio
async def test_05_test_tasks_never_enter_production_recovery():
    """Verify test tasks, test fixtures, and verification tasks are quarantined on recovery."""
    test_dir = Path(tempfile.mkdtemp(prefix="futaba_recovery_test_"))
    try:
        journal = TaskJournal(base_dir=test_dir)

        # 1. Task with explicit test origin
        t_origin = Task(
            title="Explicit test origin task",
            user_request="run tests",
            state=TaskState.RUNNING,
            origin=TaskOrigin.TEST.value,
        )
        journal.save(t_origin)

        # 2. Task with verification origin
        t_verif = Task(
            title="Verification origin task",
            user_request="verify state",
            state=TaskState.RUNNING,
            origin=TaskOrigin.VERIFICATION.value,
        )
        journal.save(t_verif)

        # 3. Legacy task with test title
        t_legacy = Task(
            title="Type Ground Truth Verification Test into Notepad",
            user_request="Type Ground Truth Verification Test into Notepad",
            state=TaskState.RUNNING,
            origin="user",  # Legacy user origin but title contains test heuristics
        )
        journal.save(t_legacy)

        # 4. Valid user task with checkpoint
        t_user = Task(
            title="User legitimate task",
            user_request="open spreadsheet",
            state=TaskState.RUNNING,
            origin=TaskOrigin.USER.value,
            recovery_policy=TaskRecoveryPolicy.AUTO_RESUME.value,
        )
        t_user.checkpoint(0, {"done": True})
        journal.save(t_user)

        tm = TaskManager(journal=journal)
        recovered = await tm.recover_from_crash()

        recovered_ids = [t.task_id for t in recovered]
        assert t_origin.task_id not in recovered_ids, "Test origin task must not be recovered"
        assert t_verif.task_id not in recovered_ids, "Verification task must not be recovered"
        assert t_legacy.task_id not in recovered_ids, "Legacy test task must not be recovered"
        assert t_user.task_id in recovered_ids, "Valid user task must be recovered"

        # Verify quarantined tasks exist in quarantine directory
        q_dir = get_quarantine_dir()
        assert (q_dir / f"{t_origin.task_id}.json").exists()
        assert (q_dir / f"{t_verif.task_id}.json").exists()
        assert (q_dir / f"{t_legacy.task_id}.json").exists()

        print("\n[PASS] Test provenance isolation and quarantine verified.")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_06_yolo_mode_not_enabled_in_production():
    """Verify that HERMES_YOLO_MODE is NOT enabled when running in production."""
    old_env = os.environ.get("FUTABA_ENV")
    os.environ["FUTABA_ENV"] = "production"
    os.environ.pop("HERMES_YOLO_MODE", None)
    try:
        from futaba.agent.hermes_bridge import HermesBridge
        bridge = HermesBridge()
        tool = HermesComputerUseTool(bridge)
        # Execute an action
        res = await tool.execute("inspect_window", {"title": "NonExistentWindow999"})
        # Verify HERMES_YOLO_MODE is NOT "1"
        assert os.environ.get("HERMES_YOLO_MODE") != "1", "HERMES_YOLO_MODE must not be set in production"
        print("\n[PASS] YOLO mode confirmed inactive in production.")
    finally:
        if old_env is not None:
            os.environ["FUTABA_ENV"] = old_env
        else:
            os.environ.pop("FUTABA_ENV", None)


@pytest.mark.asyncio
async def test_07_open_application_action_canonicalization():
    """Verify that open_application action routes cleanly without unknown action error."""
    # Test via ToolRegistry
    registry = ToolRegistry.create_default()
    result = await registry.execute(
        tool_name="computer_use",
        action="open_application",
        parameters={"name": "notepad"},
    )
    assert result.success is True or "notepad" in str(result.output).lower() or "not found" not in result.error.lower()
    assert "unknown action 'open_application'" not in result.error.lower()
    print(f"\n[PASS] open_application canonicalization verified: success={result.success}, output={result.output}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
