"""Test async components: terminal execution, resource monitoring, task persistence."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import asyncio
import tempfile
from pathlib import Path


async def test_terminal_tool():
    """Test actual command execution."""
    from futaba.agent.tools import TerminalTool

    terminal = TerminalTool()

    # Test PowerShell execution
    result = await terminal.execute("run_powershell", {"command": "echo 'Hello from Futaba'"})
    assert result.success, f"PowerShell failed: {result.error}"
    assert "Hello from Futaba" in result.output
    print(f"[PASS] PowerShell execution: {result.output.strip()}")

    # Test process listing
    result = await terminal.execute("list_processes", {"filter": "python"})
    assert result.success, f"Process listing failed: {result.error}"
    print(f"[PASS] Process listing works")

    # Test timeout
    result = await terminal.execute("run_powershell", {
        "command": "Start-Sleep -Seconds 10",
        "timeout": 2
    })
    assert not result.success
    assert "timed out" in result.error.lower()
    print(f"[PASS] Command timeout works: {result.error}")


async def test_filesystem_tool():
    """Test file operations."""
    from futaba.agent.tools import FilesystemTool

    fs = FilesystemTool()
    tmp = Path(tempfile.mkdtemp()) / "futaba_test"

    # Create directory
    result = await fs.execute("create_directory", {"path": str(tmp)})
    assert result.success
    print(f"[PASS] Create directory")

    # Write file
    test_file = tmp / "test.txt"
    result = await fs.execute("write_file", {
        "path": str(test_file),
        "content": "Hello Futaba!",
    })
    assert result.success
    print(f"[PASS] Write file")

    # Read file
    result = await fs.execute("read_file", {"path": str(test_file)})
    assert result.success
    assert result.output == "Hello Futaba!"
    print(f"[PASS] Read file: {result.output}")

    # List directory
    result = await fs.execute("list_directory", {"path": str(tmp)})
    assert result.success
    print(f"[PASS] List directory")

    # File info
    result = await fs.execute("file_info", {"path": str(test_file)})
    assert result.success
    print(f"[PASS] File info")

    # Cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


async def test_resource_monitor():
    """Test resource monitoring."""
    from futaba.performance.resource_manager import ResourceMonitor

    monitor = ResourceMonitor(sample_interval=1.0)

    # Take a single snapshot
    snap = await monitor._sample()
    print(f"[PASS] Resource snapshot:")
    print(f"  CPU: {snap.cpu_percent:.1f}%")
    print(f"  RAM: {snap.ram_percent:.1f}% ({snap.ram_available_mb:.0f} MB free)")
    print(f"  GPU available: {snap.gpu_available}")
    if snap.gpu_available:
        print(f"  GPU: {snap.gpu_name} ({snap.gpu_utilization:.0f}%)")
    print(f"  Foreground: {snap.foreground_process}")
    print(f"  Fullscreen: {snap.is_fullscreen}")
    print(f"  Gaming: {snap.is_gaming}")
    print(f"  Load level: {snap.load_level.value}")


async def test_task_persistence():
    """Test task journal round-trip."""
    from futaba.tasks.task_manager import Task, TaskState, TaskJournal, ExecutionPlan, ExecutionStep

    tmp = Path(tempfile.mkdtemp()) / "tasks"
    journal = TaskJournal(base_dir=tmp)

    # Create and persist a task
    task = Task(user_request="Build a website", title="Build Website")
    task.plan = ExecutionPlan(
        objective="Build a website",
        steps=[
            ExecutionStep(description="Create HTML", tool="filesystem", action="write_file"),
            ExecutionStep(description="Create CSS", tool="filesystem", action="write_file"),
        ],
    )
    task.transition_to(TaskState.PLANNING, "Generating plan")
    task.transition_to(TaskState.RUNNING, "Executing")
    task.checkpoint(0)

    journal.save(task)
    print(f"[PASS] Task persisted: {task.task_id[:8]}")

    # Load it back
    loaded = journal.load(task.task_id)
    assert loaded is not None
    assert loaded.state == TaskState.RUNNING
    assert loaded.plan is not None
    assert len(loaded.plan.steps) == 2
    assert len(loaded.state_history) == 2
    step_idx, _ = loaded.get_recovery_point()
    assert step_idx == 0
    print(f"[PASS] Task loaded: state={loaded.state.value}, steps={len(loaded.plan.steps)}")

    # Test recovery detection
    active = journal.get_active_tasks()
    assert len(active) == 1
    recoverable = journal.get_recoverable_tasks()
    assert len(recoverable) == 1
    print(f"[PASS] Recovery detection: {len(recoverable)} recoverable tasks")

    # Cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


async def test_application_tool():
    """Test application discovery."""
    from futaba.agent.tools import ApplicationTool

    app_tool = ApplicationTool()

    # List running applications
    result = await app_tool.execute("list_running", {})
    assert result.success, f"List running failed: {result.error}"
    print(f"[PASS] Running applications listed")

    # Discover Chrome
    result = await app_tool.execute("discover", {"query": "Chrome"})
    print(f"[PASS] App discovery executed (success={result.success})")


async def test_tool_registry_execution():
    """Test tool registry timeout and dispatch."""
    from futaba.agent.tools import ToolRegistry

    registry = ToolRegistry.create_default()

    # Execute through registry with timeout
    result = await registry.execute(
        tool_name="terminal",
        action="run_powershell",
        parameters={"command": "Get-Date"},
        timeout=10,
    )
    assert result.success
    print(f"[PASS] Registry execution: {result.output.strip()}")

    # Test unknown tool
    result = await registry.execute(
        tool_name="nonexistent",
        action="whatever",
        parameters={},
    )
    assert not result.success
    assert "Unknown tool" in result.error
    print(f"[PASS] Unknown tool handled: {result.error}")


async def main():
    print("=" * 50)
    print("FUTABA ASYNC COMPONENT TESTS")
    print("=" * 50)
    print()

    await test_terminal_tool()
    print()
    await test_filesystem_tool()
    print()
    await test_resource_monitor()
    print()
    await test_task_persistence()
    print()
    await test_application_tool()
    print()
    await test_tool_registry_execution()

    print()
    print("=" * 50)
    print("ALL ASYNC TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
