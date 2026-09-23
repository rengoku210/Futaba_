"""
Futaba Task System — Persistent, recoverable task state machine.

This is the central orchestration component of Futaba. Every user request
becomes a Task that moves through a well-defined state machine with:

- Durable persistence (survives crashes/restarts)
- Atomic state transitions with journaling
- Structured execution plans with step-level tracking
- Intelligent crash recovery and resumption
- Configurable retry policies
- Cancellation support
- Blocker detection and user interaction

Architecture:
    User Request → TaskManager.create_task() → Task(QUEUED)
        → Planner generates ExecutionPlan → Task(PLANNING→RUNNING)
        → Steps execute through Hermes → Task(RUNNING)
        → On error → Task(RECOVERING) → retry/fallback → Task(RUNNING)
        → On blocker → Task(BLOCKED) → user provides info → Task(RUNNING)
        → Verification → Task(VERIFYING→COMPLETED)
        → On fatal failure → Task(FAILED)

Persistence:
    Tasks are journaled to %LOCALAPPDATA%\\Futaba\\tasks\\<task_id>.json
    State transitions are atomic — the journal is written before the
    transition takes effect, so a crash at any point can be recovered.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

from futaba.core.config import get_tasks_dir, get_quarantine_dir

logger = logging.getLogger("futaba.tasks")


# ---------------------------------------------------------------------------
# Task Origin & Recovery Policy
# ---------------------------------------------------------------------------

class TaskOrigin(str, enum.Enum):
    """Provenance origin of a task."""
    USER = "user"
    VOICE = "voice"
    UI = "ui"
    SCHEDULED = "scheduled"
    AUTOMATION = "automation"
    TEST = "test"
    TEST_FIXTURE = "test_fixture"
    DEVELOPMENT = "development"
    VERIFICATION = "verification"


class TaskRecoveryPolicy(str, enum.Enum):
    """Crash recovery policy for a task."""
    AUTO_RESUME = "auto_resume"
    CONFIRM = "confirm"
    QUARANTINE = "quarantine"
    DISCARD = "discard"


def is_test_environment() -> bool:
    """Detect if current execution is within a test runner or test environment."""
    return (
        os.environ.get("FUTABA_ENV") == "test"
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


# ---------------------------------------------------------------------------
# Task State Machine
# ---------------------------------------------------------------------------

class TaskState(str, enum.Enum):
    """
    Task lifecycle states. Transitions are enforced by the state machine.

    State diagram:
        QUEUED → PLANNING → RUNNING → VERIFYING → COMPLETED
                    ↓          ↓↑         ↓
                  FAILED    RECOVERING  FAILED
                              ↓
                           BLOCKED → (user input) → RUNNING
                              ↓
                           FAILED

        Any state → CANCELLED (user-initiated)
    """
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting"          # Waiting for a sub-task or external event
    BLOCKED = "blocked"          # Needs user input
    RECOVERING = "recovering"    # Attempting error recovery
    VERIFYING = "verifying"      # Verifying task completion
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (
            TaskState.QUEUED, TaskState.PLANNING, TaskState.RUNNING,
            TaskState.WAITING, TaskState.RECOVERING, TaskState.VERIFYING
        )


# Valid state transitions
_VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.QUEUED:      {TaskState.PLANNING, TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.PLANNING:    {TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RUNNING:     {TaskState.VERIFYING, TaskState.WAITING, TaskState.BLOCKED, TaskState.RECOVERING, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.WAITING:     {TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.BLOCKED:     {TaskState.RUNNING, TaskState.PLANNING, TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RECOVERING:  {TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.VERIFYING:   {TaskState.COMPLETED, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.COMPLETED:   set(),  # Terminal
    TaskState.FAILED:      {TaskState.QUEUED, TaskState.RECOVERING},  # Can be retried from scratch or recovered
    TaskState.CANCELLED:   set(),  # Terminal
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


# ---------------------------------------------------------------------------
# Execution Plan & Steps
# ---------------------------------------------------------------------------

class StepState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ExecutionStep:
    """A single step in an execution plan."""
    step_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    index: int = 0
    description: str = ""
    tool: str = ""            # Which tool/capability to use
    action: str = ""          # Specific action within the tool
    parameters: dict[str, Any] = field(default_factory=dict)
    state: StepState = StepState.PENDING
    result: Any = None
    error: str = ""
    retries: int = 0
    max_retries: int = 3
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    depends_on: list[str] = field(default_factory=list)  # step_ids
    verification: str = ""    # How to verify this step succeeded
    recovery_strategy: str = ""  # What to do if this step fails
    idempotent: bool = True   # Safe to re-execute after crash?
    destructive: bool = False # Requires autonomy policy check?

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionStep":
        d = dict(d)
        d["state"] = StepState(d.get("state", "pending"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ExecutionPlan:
    """A structured plan for executing a task."""
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    objective: str = ""
    context: str = ""
    requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    steps: list[ExecutionStep] = field(default_factory=list)
    current_step_index: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    validation_criteria: list[str] = field(default_factory=list)
    estimated_duration_minutes: float = 0.0

    @property
    def current_step(self) -> ExecutionStep | None:
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    @property
    def completed_steps(self) -> list[ExecutionStep]:
        return [s for s in self.steps if s.state == StepState.COMPLETED]

    @property
    def failed_steps(self) -> list[ExecutionStep]:
        return [s for s in self.steps if s.state == StepState.FAILED]

    @property
    def pending_steps(self) -> list[ExecutionStep]:
        return [s for s in self.steps if s.state == StepState.PENDING]

    @property
    def progress(self) -> float:
        if not self.steps:
            return 0.0
        return len(self.completed_steps) / len(self.steps)

    def advance(self) -> ExecutionStep | None:
        """Move to the next pending step. Returns it, or None if done."""
        for i, step in enumerate(self.steps):
            if step.state == StepState.PENDING:
                self.current_step_index = i
                return step
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionPlan":
        d = dict(d)
        raw_steps = d.pop("steps", [])
        plan = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        plan.steps = [ExecutionStep.from_dict(s) for s in raw_steps]
        return plan


# ---------------------------------------------------------------------------
# Blocker
# ---------------------------------------------------------------------------

@dataclass
class Blocker:
    """Represents something that prevents task progress."""
    blocker_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    kind: str = ""       # missing_file, missing_key, missing_permission, unclear_intent, etc.
    description: str = ""
    attempted_resolutions: list[str] = field(default_factory=list)
    user_prompt: str = ""  # Concise message to show the user
    resolved: bool = False
    resolution: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Blocker":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """
    A persistent, recoverable task.

    Tasks are the primary unit of work in Futaba. Each user request
    creates a Task, which is planned, executed, verified, and completed
    (or fails/is cancelled).

    Tasks are journaled to disk so they survive crashes and restarts.
    """
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    user_request: str = ""
    state: TaskState = TaskState.QUEUED
    plan: ExecutionPlan | None = None
    blocker: Blocker | None = None

    # Provenance & Recovery Policy
    origin: str = "user"
    session_id: str = ""
    owner: str = "production"
    recovery_policy: str = "auto_resume"
    quarantine_reason: str = ""

    # Tracking
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str = ""
    completed_at: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Execution metadata
    model_used: str = ""
    tools_used: list[str] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 5
    total_duration_seconds: float = 0.0

    # Result
    result: str = ""
    result_artifacts: list[str] = field(default_factory=list)
    verification_status: str = ""
    verification_evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    # Recovery state — used to resume after crash
    _last_known_good_step: int = -1
    _checkpoint_data: dict[str, Any] = field(default_factory=dict)

    # State transition log
    state_history: list[dict[str, str]] = field(default_factory=list)

    def transition_to(self, new_state: TaskState, reason: str = "") -> None:
        """
        Atomically transition to a new state.
        Raises InvalidTransitionError if the transition is not valid.
        Self-transitions (state == new_state) are idempotent no-ops.
        """
        if new_state == self.state:
            logger.debug("Task %s already in state %s; transition is no-op", self.task_id[:8], new_state.value)
            return

        valid = _VALID_TRANSITIONS.get(self.state, set())
        if new_state not in valid:
            raise InvalidTransitionError(
                f"Cannot transition from {self.state.value} to {new_state.value}. "
                f"Valid transitions: {[s.value for s in valid]}"
            )

        old_state = self.state
        now = datetime.now(timezone.utc).isoformat()

        self.state_history.append({
            "from": old_state.value,
            "to": new_state.value,
            "at": now,
            "reason": reason,
        })
        self.state = new_state
        self.updated_at = now

        if new_state == TaskState.RUNNING and not self.started_at:
            self.started_at = now
        elif new_state.is_terminal:
            self.completed_at = now

        logger.info(
            "Task %s [%s]: %s → %s%s",
            self.task_id[:8], self.title[:30],
            old_state.value, new_state.value,
            f" ({reason})" if reason else ""
        )

    def set_blocker(self, blocker: Blocker) -> None:
        """Set a blocker and transition to BLOCKED state."""
        self.blocker = blocker
        self.transition_to(TaskState.BLOCKED, reason=blocker.description)

    def resolve_blocker(self, resolution: str) -> None:
        """Resolve the current blocker and resume execution."""
        if self.blocker:
            self.blocker.resolved = True
            self.blocker.resolution = resolution
            self.blocker.resolved_at = datetime.now(timezone.utc).isoformat()
        self.transition_to(TaskState.RUNNING, reason=f"Blocker resolved: {resolution}")

    def checkpoint(self, step_index: int, data: dict[str, Any] | None = None) -> None:
        """Save a checkpoint for crash recovery."""
        self._last_known_good_step = step_index
        if data:
            self._checkpoint_data = data
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def get_recovery_point(self) -> tuple[int, dict[str, Any]]:
        """Get the last checkpoint for resuming after crash."""
        return self._last_known_good_step, self._checkpoint_data

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dictionary."""
        d = {
            "task_id": self.task_id,
            "title": self.title,
            "user_request": self.user_request,
            "state": self.state.value,
            "plan": self.plan.to_dict() if self.plan else None,
            "blocker": self.blocker.to_dict() if self.blocker else None,
            "origin": self.origin,
            "session_id": self.session_id,
            "owner": self.owner,
            "recovery_policy": self.recovery_policy,
            "quarantine_reason": self.quarantine_reason,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "updated_at": self.updated_at,
            "model_used": self.model_used,
            "tools_used": self.tools_used,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "total_duration_seconds": self.total_duration_seconds,
            "result": self.result,
            "result_artifacts": self.result_artifacts,
            "verification_status": self.verification_status,
            "verification_evidence": self.verification_evidence,
            "error": self.error,
            "_last_known_good_step": self._last_known_good_step,
            "_checkpoint_data": self._checkpoint_data,
            "state_history": self.state_history,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        """Deserialize from a dictionary."""
        d = dict(d)
        d["state"] = TaskState(d.get("state", "queued"))
        raw_plan = d.pop("plan", None)
        raw_blocker = d.pop("blocker", None)

        # Filter to known fields only
        known = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in d.items() if k in known}

        task = cls(**filtered)
        if raw_plan:
            task.plan = ExecutionPlan.from_dict(raw_plan)
        if raw_blocker:
            task.blocker = Blocker.from_dict(raw_blocker)
        return task


# ---------------------------------------------------------------------------
# Task Persistence (Journal)
# ---------------------------------------------------------------------------

class TaskJournal:
    """
    Persistent task storage with atomic writes.

    Each task is stored as a JSON file. Writes use atomic rename to
    prevent corruption on crash.
    """

    def __init__(self, base_dir: Path | None = None):
        self._dir = base_dir or get_tasks_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def save(self, task: Task) -> None:
        """Atomically persist a task to disk."""
        path = self._task_path(task.task_id)
        tmp_path = path.with_suffix(".tmp")
        try:
            data = json.dumps(task.to_dict(), indent=2, default=str)
            tmp_path.write_text(data, encoding="utf-8")
            # Atomic rename on Windows (replace if exists)
            if path.exists():
                path.unlink()
            tmp_path.rename(path)
        except Exception as e:
            logger.error("Failed to persist task %s: %s", task.task_id[:8], e)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def load(self, task_id: str) -> Task | None:
        """Load a task from disk."""
        path = self._task_path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Task.from_dict(data)
        except Exception as e:
            logger.error("Failed to load task %s: %s", task_id[:8], e)
            return None

    def load_all(self) -> list[Task]:
        """Load all tasks from disk."""
        tasks = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                tasks.append(Task.from_dict(data))
            except Exception as e:
                logger.warning("Skipping corrupt task file %s: %s", path.name, e)
        return tasks

    def delete(self, task_id: str) -> None:
        """Delete a task from disk."""
        path = self._task_path(task_id)
        path.unlink(missing_ok=True)

    def quarantine_task(self, task: Task, reason: str) -> Path:
        """
        Quarantine a task by moving its JSON record to the quarantine directory
        and removing it from the active tasks directory.
        """
        quarantine_dir = get_quarantine_dir()
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        task.quarantine_reason = reason
        task.recovery_policy = TaskRecoveryPolicy.QUARANTINE.value
        q_path = quarantine_dir / f"{task.task_id}.json"
        try:
            data = json.dumps(task.to_dict(), indent=2, default=str)
            q_path.write_text(data, encoding="utf-8")
            active_path = self._task_path(task.task_id)
            if active_path.exists():
                active_path.unlink(missing_ok=True)
            logger.warning(
                "Quarantined task %s [%s]: %s -> %s",
                task.task_id[:8], task.title[:30], reason, q_path
            )
        except Exception as e:
            logger.error("Failed to quarantine task %s: %s", task.task_id[:8], e)
        return q_path

    def get_active_tasks(self) -> list[Task]:
        """Get all tasks that were active (not terminal) when last saved."""
        return [t for t in self.load_all() if t.state.is_active]

    def get_recoverable_tasks(self) -> list[Task]:
        """
        Get tasks that need crash recovery.

        A task is recoverable if it was in an active state and has a
        checkpoint from which execution can resume.
        """
        recoverable = []
        for task in self.get_active_tasks():
            step_idx, _ = task.get_recovery_point()
            if step_idx >= 0 or task.state == TaskState.QUEUED:
                recoverable.append(task)
        return recoverable


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------

class TaskQueue:
    """
    Priority-based async task queue with pause/resume/cancel.

    Tasks are executed in priority order (lower number = higher priority).
    The queue supports concurrent execution with configurable concurrency.
    """

    def __init__(self, max_concurrent: int = 3):
        self._queue: asyncio.PriorityQueue[tuple[int, float, str]] = asyncio.PriorityQueue()
        self._tasks: dict[str, Task] = {}
        self._max_concurrent = max_concurrent
        self._running_count = 0
        self._paused = False
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, task: Task, priority: int = 5) -> None:
        """Add a task to the queue."""
        async with self._lock:
            self._tasks[task.task_id] = task
            self._cancel_events[task.task_id] = asyncio.Event()
            await self._queue.put((priority, time.time(), task.task_id))
            logger.info("Task %s enqueued (priority=%d)", task.task_id[:8], priority)

    async def dequeue(self) -> Task | None:
        """Get the next task to execute (blocks until available)."""
        while True:
            if self._paused:
                await asyncio.sleep(0.5)
                continue

            if self._running_count >= self._max_concurrent:
                await asyncio.sleep(0.1)
                continue

            try:
                priority, _, task_id = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            async with self._lock:
                task = self._tasks.get(task_id)
                if task and not task.state.is_terminal:
                    self._running_count += 1
                    return task
                continue

    def mark_done(self, task_id: str) -> None:
        """Mark a task as no longer running."""
        self._running_count = max(0, self._running_count - 1)

    def cancel(self, task_id: str) -> None:
        """Signal cancellation for a task."""
        event = self._cancel_events.get(task_id)
        if event:
            event.set()

    def is_cancelled(self, task_id: str) -> bool:
        """Check if a task has been cancelled."""
        event = self._cancel_events.get(task_id)
        return event.is_set() if event else False

    def pause(self) -> None:
        self._paused = True
        logger.info("Task queue paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Task queue resumed")

    @property
    def active_count(self) -> int:
        return self._running_count

    @property
    def queued_count(self) -> int:
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# Task Manager
# ---------------------------------------------------------------------------

class TaskManager:
    """
    Central task lifecycle manager.

    Responsibilities:
    - Create tasks from user requests
    - Manage task state transitions
    - Persist tasks durably
    - Recover tasks after crash
    - Coordinate execution through the task queue
    - Track active tasks

    This is the primary interface that the Futaba agent controller uses.
    """

    def __init__(self, journal: TaskJournal | None = None):
        self._journal = journal or TaskJournal()
        self._queue = TaskQueue()
        self._active_tasks: dict[str, Task] = {}
        self._listeners: list[Callable[[Task, TaskState, TaskState], Awaitable[None]]] = []
        self._lock = asyncio.Lock()

    async def create_task(
        self,
        user_request: str,
        title: str = "",
        priority: int = 5,
        origin: str | None = None,
        session_id: str = "",
        owner: str = "production",
        recovery_policy: str | None = None,
    ) -> Task:
        """
        Create a new task from a user request.

        The task is immediately persisted and enqueued for execution.
        Provenance (origin, session_id, owner, recovery_policy) is durably recorded.
        """
        if origin is None:
            origin = TaskOrigin.TEST.value if is_test_environment() else TaskOrigin.USER.value
        if recovery_policy is None:
            recovery_policy = (
                TaskRecoveryPolicy.QUARANTINE.value
                if origin in (
                    TaskOrigin.TEST.value,
                    TaskOrigin.TEST_FIXTURE.value,
                    TaskOrigin.DEVELOPMENT.value,
                    TaskOrigin.VERIFICATION.value,
                )
                else TaskRecoveryPolicy.AUTO_RESUME.value
            )

        task = Task(
            user_request=user_request,
            title=title or self._generate_title(user_request),
            origin=origin,
            session_id=session_id,
            owner=owner,
            recovery_policy=recovery_policy,
        )

        # Persist before queueing — if we crash between persist and queue,
        # recovery will pick it up according to its recovery policy
        self._journal.save(task)

        async with self._lock:
            self._active_tasks[task.task_id] = task

        await self._queue.enqueue(task, priority)
        logger.info("Created task %s (origin=%s): %s", task.task_id[:8], origin, task.title)
        return task

    async def transition(
        self,
        task_id: str,
        new_state: TaskState,
        reason: str = "",
    ) -> Task:
        """
        Transition a task to a new state.

        The transition is:
        1. Validated against the state machine
        2. Applied to the in-memory task
        3. Persisted to the journal (atomic write)
        4. Listeners are notified

        If step 3 fails, the in-memory state is rolled back.
        """
        async with self._lock:
            task = self._active_tasks.get(task_id)
            if not task:
                task = self._journal.load(task_id)
                if not task:
                    raise ValueError(f"Task {task_id} not found")
                self._active_tasks[task_id] = task

            old_state = task.state
            task.transition_to(new_state, reason)

            # Persist atomically — rollback on failure
            try:
                self._journal.save(task)
            except Exception:
                # Rollback in-memory state
                task.state = old_state
                task.state_history.pop()
                raise

            if new_state.is_terminal:
                self._queue.mark_done(task_id)

        # Notify listeners outside the lock
        for listener in self._listeners:
            try:
                await listener(task, old_state, new_state)
            except Exception as e:
                logger.warning("Listener error: %s", e)

        return task

    async def update_plan(self, task_id: str, plan: ExecutionPlan) -> Task:
        """Set or update a task's execution plan."""
        async with self._lock:
            task = self._get_task(task_id)
            task.plan = plan
            task.updated_at = datetime.now(timezone.utc).isoformat()
            self._journal.save(task)
        return task

    async def checkpoint(
        self,
        task_id: str,
        step_index: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Save a crash-recovery checkpoint for a task."""
        async with self._lock:
            task = self._get_task(task_id)
            task.checkpoint(step_index, data)
            self._journal.save(task)

    async def set_blocker(self, task_id: str, blocker: Blocker) -> Task:
        """Block a task and request user input."""
        async with self._lock:
            task = self._get_task(task_id)
            task.set_blocker(blocker)
            self._journal.save(task)
        return task

    block_task = set_blocker

    async def resolve_blocker(self, task_id: str, resolution: str) -> Task:
        """Resolve a blocker and resume execution."""
        async with self._lock:
            task = self._get_task(task_id)
            task.resolve_blocker(resolution)
            self._journal.save(task)
        # Re-enqueue for execution
        await self._queue.enqueue(task, priority=3)  # Higher priority for resumed tasks
        return task

    async def cancel_task(self, task_id: str) -> Task:
        """Cancel a task."""
        self._queue.cancel(task_id)
        return await self.transition(task_id, TaskState.CANCELLED, "User cancelled")

    async def complete_task(self, task_id: str, result: str, artifacts: list[str] | None = None) -> Task:
        """Mark a task as completed with its result."""
        async with self._lock:
            task = self._get_task(task_id)
            task.result = result
            task.result_artifacts = artifacts or []
            task.completed_at = datetime.now(timezone.utc).isoformat()
            if task.started_at:
                start = datetime.fromisoformat(task.started_at)
                end = datetime.fromisoformat(task.completed_at)
                task.total_duration_seconds = (end - start).total_seconds()
        return await self.transition(task_id, TaskState.COMPLETED, "Task completed")

    async def fail_task(self, task_id: str, error: str) -> Task:
        """Mark a task as failed."""
        async with self._lock:
            task = self._get_task(task_id)
            if task.state == TaskState.FAILED:
                task.error = error
                self._journal.save(task)
                return task
            task.error = error
        return await self.transition(task_id, TaskState.FAILED, error)

    async def recover_from_crash(self) -> list[Task]:
        """
        Recover tasks that were active when the application crashed.

        Safety Rules:
        1. Test, fixture, development, and verification tasks are strictly isolated
           and quarantined; they are never executed on production startup.
        2. Stale queued tasks from prior sessions are quarantined or held for explicit
           user confirmation, never automatically executing background side-effects.
        3. Only valid active user tasks with checkpoints or safe recovery policy
           are resumed.

        Logs structured recovery summary with metrics:
        scanned, recoverable_user_tasks, stale_user_tasks, test_tasks, invalid_tasks,
        resumed, quarantined.
        """
        active_candidates = self._journal.get_active_tasks()
        scanned = len(active_candidates)

        recoverable_user: list[Task] = []
        stale_user: list[Task] = []
        test_tasks: list[Task] = []
        invalid_tasks: list[Task] = []
        quarantined_count = 0
        recovered: list[Task] = []

        for task in active_candidates:
            # Check 1: Invalid task data
            if not task.task_id or (not task.title and not task.user_request):
                invalid_tasks.append(task)
                self._journal.quarantine_task(task, "Corrupt or invalid task metadata")
                quarantined_count += 1
                continue

            # Check 2: Test task isolation (origin or legacy heuristic markers)
            is_test = False
            reason = ""
            if task.origin in (
                TaskOrigin.TEST.value,
                TaskOrigin.TEST_FIXTURE.value,
                TaskOrigin.DEVELOPMENT.value,
                TaskOrigin.VERIFICATION.value,
            ):
                is_test = True
                reason = f"Test provenance origin='{task.origin}'"
            else:
                title_lower = (task.title or "").lower()
                req_lower = (task.user_request or "").lower()
                if (
                    "verification test" in title_lower
                    or "verification test" in req_lower
                    or "ground truth" in title_lower
                    or "text that does not exist" in title_lower
                    or title_lower.startswith("test ")
                    or "pytest" in title_lower
                ):
                    is_test = True
                    reason = f"Legacy test task signature in title/request: '{task.title}'"

            if is_test:
                test_tasks.append(task)
                self._journal.quarantine_task(task, f"Isolated test task: {reason}")
                quarantined_count += 1
                continue

            # Check 3: Explicit quarantine policy
            if task.recovery_policy == TaskRecoveryPolicy.QUARANTINE.value:
                test_tasks.append(task)
                self._journal.quarantine_task(task, "Task policy is explicitly set to quarantine")
                quarantined_count += 1
                continue

            # Check 4: Stale queued tasks (unstarted queued tasks from prior sessions)
            if task.state == TaskState.QUEUED:
                is_stale = False
                try:
                    created_dt = datetime.fromisoformat(task.created_at)
                    age_s = (datetime.now(timezone.utc) - created_dt).total_seconds()
                    # Older than 15 minutes or explicit confirm policy
                    if age_s > 900 or task.recovery_policy == TaskRecoveryPolicy.CONFIRM.value:
                        is_stale = True
                except Exception:
                    is_stale = True

                if is_stale:
                    stale_user.append(task)
                    self._journal.quarantine_task(
                        task,
                        "Stale queued task from prior session (requires user re-confirmation)"
                    )
                    quarantined_count += 1
                    continue

            # Check 5: Recoverable user task
            step_idx, _ = task.get_recovery_point()
            if step_idx >= 0 or task.state in (TaskState.RUNNING, TaskState.RECOVERING, TaskState.WAITING):
                recoverable_user.append(task)
            else:
                stale_user.append(task)
                self._journal.quarantine_task(task, "Stale active task without recovery checkpoint")
                quarantined_count += 1

        # Now resume recoverable user tasks
        for task in recoverable_user:
            try:
                recovery_step, checkpoint_data = task.get_recovery_point()

                if task.state == TaskState.QUEUED:
                    async with self._lock:
                        self._active_tasks[task.task_id] = task
                    await self._queue.enqueue(task)
                    recovered.append(task)
                    logger.info("Resumed recent queued user task %s: %s", task.task_id[:8], task.title)
                    continue

                if task.plan and recovery_step >= 0:
                    for i, step in enumerate(task.plan.steps):
                        if i > recovery_step and step.state == StepState.RUNNING:
                            if step.idempotent:
                                step.state = StepState.PENDING
                                step.retries += 1
                            else:
                                step.state = StepState.PENDING
                                logger.warning(
                                    "Task %s step %d was non-idempotent and crashed mid-execution. Needs verification.",
                                    task.task_id[:8], i
                                )
                    task.plan.current_step_index = recovery_step + 1

                if task.state not in (TaskState.BLOCKED, TaskState.QUEUED):
                    task.state = TaskState.RECOVERING
                    task.state_history.append({
                        "from": "crash",
                        "to": TaskState.RECOVERING.value,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "reason": "crash recovery",
                    })

                self._journal.save(task)
                async with self._lock:
                    self._active_tasks[task.task_id] = task
                await self._queue.enqueue(task, priority=2)
                recovered.append(task)
                logger.info("Recovered task %s from step %d: %s", task.task_id[:8], recovery_step, task.title)

            except Exception as e:
                logger.error("Failed to recover task %s: %s", task.task_id[:8], traceback.format_exc())

        logger.info(
            "Startup recovery summary: scanned=%d, recoverable_user_tasks=%d, stale_user_tasks=%d, test_tasks=%d, invalid_tasks=%d, resumed=%d, quarantined=%d",
            scanned, len(recoverable_user), len(stale_user), len(test_tasks), len(invalid_tasks), len(recovered), quarantined_count
        )

        return recovered

    def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID (from memory or disk)."""
        task = self._active_tasks.get(task_id)
        if not task:
            task = self._journal.load(task_id)
        return task

    def get_active_tasks(self) -> list[Task]:
        """Get all currently active tasks."""
        return [t for t in self._active_tasks.values() if t.state.is_active]

    def get_all_tasks(self) -> list[Task]:
        """Get all tasks (active and historical)."""
        return self._journal.load_all()

    def add_listener(
        self,
        callback: Callable[[Task, TaskState, TaskState], Awaitable[None]],
    ) -> None:
        """Add a state-transition listener."""
        self._listeners.append(callback)

    def _get_task(self, task_id: str) -> Task:
        """Get a task, raising if not found. Must be called under lock."""
        task = self._active_tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found in active tasks")
        return task

    def _generate_title(self, request: str) -> str:
        """Generate a concise title from a user request."""
        # Simple heuristic — the agent can set a better title later
        title = request.strip()
        if len(title) > 60:
            title = title[:57] + "..."
        return title
