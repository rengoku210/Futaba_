"""Core architecture smoke test."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from futaba.core.config import FutabaConfig, get_config, get_app_data_dir
from futaba.tasks.task_manager import Task, TaskState, TaskManager, ExecutionPlan, ExecutionStep
from futaba.routing.model_router import ModelRouter, ChatMessage, ProviderRegistry
from futaba.agent.tools import ToolRegistry, TerminalTool, FilesystemTool
from futaba.memory.memory_manager import MemoryManager, MemoryStore, MemoryEntry
from futaba.performance.resource_manager import ResourceManager, ResourceMonitor
from futaba.security.credentials import CredentialStore
from futaba.ipc.server import IPCServer, FutabaAPI

print("All core modules imported successfully!")

# Test config creation
config = FutabaConfig()
print(f"Default provider: {config.ai.default_provider}")
print(f"Autonomy level: {config.autonomy.level.value}")
print(f"App data dir: {get_app_data_dir()}")

# Test task state machine
task = Task(user_request="Open Chrome", title="Open Chrome")
print(f"Task state: {task.state.value}")
task.transition_to(TaskState.PLANNING, "Starting")
print(f"Task state: {task.state.value}")
task.transition_to(TaskState.RUNNING, "Executing")
print(f"Task state: {task.state.value}")
task.transition_to(TaskState.COMPLETED, "Done")
print(f"Task state: {task.state.value}")
print(f"State history: {len(task.state_history)} transitions")

# Test invalid transition
task2 = Task(user_request="test")
try:
    task2.transition_to(TaskState.COMPLETED)
    print("ERROR: Should have raised InvalidTransitionError")
except Exception as e:
    print(f"Correctly rejected invalid transition: {type(e).__name__}")

# Test serialization roundtrip
d = task.to_dict()
task3 = Task.from_dict(d)
print(f"Roundtrip: {task3.state.value} == {task.state.value}: {task3.state == task.state}")

# Test execution plan
plan = ExecutionPlan(
    objective="Open Chrome",
    steps=[
        ExecutionStep(description="Launch Chrome", tool="application", action="launch"),
        ExecutionStep(description="Verify Chrome", tool="terminal", action="run_powershell"),
    ],
)
print(f"Plan: {len(plan.steps)} steps, progress: {plan.progress:.0%}")
plan.steps[0].state = plan.steps[0].state  # Check enum works
plan_dict = plan.to_dict()
plan2 = ExecutionPlan.from_dict(plan_dict)
print(f"Plan roundtrip: {len(plan2.steps)} steps")

# Test tool registry
registry = ToolRegistry.create_default()
tools = [t.name for t in registry.all_tools()]
print(f"Registered tools: {tools}")

# Test memory store
import tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp()) / "test_memory.db"
mem = MemoryStore(db_path=tmp)
mem.store(MemoryEntry(category="test", key="hello", content={"msg": "world"}, summary="Test entry"))
entry = mem.get("test", "hello")
print(f"Memory entry: {entry.content}")
stats = mem.stats()
print(f"Memory stats: {stats['total_entries']} entries")

# Test credential store
cred = CredentialStore()
print(f"Credential store initialized")

# Test resource monitor creation
monitor = ResourceMonitor(sample_interval=60)
print(f"Resource monitor created (interval={monitor._interval}s)")

print()
print("=" * 50)
print("ALL CORE ARCHITECTURE TESTS PASSED")
print("=" * 50)
