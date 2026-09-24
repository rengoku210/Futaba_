"""
Futaba Latency & Execution Telemetry.

Measures and records timing at every stage of request lifecycle:
- speech_end
- intent_detected
- context_snapshot
- entity_resolution
- surface_selection
- planner_start, planner_end
- tool_start, tool_end
- verification_start, verification_end
- response_start, response_audio_start

Calculates:
- intent_latency
- context_latency
- planning_latency
- tool_latency
- verification_latency
- total_action_latency
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("futaba.intelligence.telemetry")


@dataclass
class LatencyRecord:
    """Telemetry timestamps and derived durations for a single turn/action."""
    action_id: str
    instruction: str = ""
    tier: str = "TIER_0"  # TIER_0, TIER_1, TIER_2
    surface: str = "DIRECT"

    # Raw timestamps (monotonic seconds)
    speech_end: float = 0.0
    intent_detected: float = 0.0
    context_snapshot: float = 0.0
    entity_resolution: float = 0.0
    surface_selection: float = 0.0
    planner_start: float = 0.0
    planner_end: float = 0.0
    tool_start: float = 0.0
    tool_end: float = 0.0
    verification_start: float = 0.0
    verification_end: float = 0.0
    response_start: float = 0.0
    response_audio_start: float = 0.0
    completed_at: float = 0.0

    # Status & metadata
    success: bool = False
    verified: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # Derived latencies in milliseconds
    @property
    def intent_latency_ms(self) -> float:
        if self.speech_end > 0 and self.intent_detected > self.speech_end:
            return (self.intent_detected - self.speech_end) * 1000.0
        return 0.0

    @property
    def context_latency_ms(self) -> float:
        if self.intent_detected > 0 and self.context_snapshot > self.intent_detected:
            return (self.context_snapshot - self.intent_detected) * 1000.0
        return 0.0

    @property
    def planning_latency_ms(self) -> float:
        if self.planner_start > 0 and self.planner_end > self.planner_start:
            return (self.planner_end - self.planner_start) * 1000.0
        return 0.0

    @property
    def tool_latency_ms(self) -> float:
        if self.tool_start > 0 and self.tool_end > self.tool_start:
            return (self.tool_end - self.tool_start) * 1000.0
        return 0.0

    @property
    def verification_latency_ms(self) -> float:
        if self.verification_start > 0 and self.verification_end > self.verification_start:
            return (self.verification_end - self.verification_start) * 1000.0
        return 0.0

    @property
    def total_action_latency_ms(self) -> float:
        start = self.speech_end or self.intent_detected or self.tool_start
        end = max(self.completed_at, self.verification_end, self.tool_end)
        if start > 0 and end > start:
            return (end - start) * 1000.0
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "instruction": self.instruction,
            "tier": self.tier,
            "surface": self.surface,
            "success": self.success,
            "verified": self.verified,
            "error": self.error,
            "latencies_ms": {
                "intent": round(self.intent_latency_ms, 2),
                "context": round(self.context_latency_ms, 2),
                "planning": round(self.planning_latency_ms, 2),
                "tool": round(self.tool_latency_ms, 2),
                "verification": round(self.verification_latency_ms, 2),
                "total": round(self.total_action_latency_ms, 2),
            },
            "metadata": self.metadata,
        }


class LatencyTelemetry:
    """Manages telemetry collection across FUTABA."""

    def __init__(self, max_history: int = 100):
        self._history: list[LatencyRecord] = []
        self._max_history = max_history

    def start_action(self, action_id: str, instruction: str = "", tier: str = "TIER_0") -> LatencyRecord:
        now = time.monotonic()
        rec = LatencyRecord(
            action_id=action_id,
            instruction=instruction,
            tier=tier,
            speech_end=now,
        )
        self._history.append(rec)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        return rec

    def record_completed(self, record: LatencyRecord, success: bool, verified: bool = True, error: str = "") -> None:
        record.completed_at = time.monotonic()
        record.success = success
        record.verified = verified
        record.error = error
        logger.info(
            "[TELEMETRY] action=%s tier=%s total=%.1fms tool=%.1fms verif=%.1fms success=%s verified=%s",
            record.action_id,
            record.tier,
            record.total_action_latency_ms,
            record.tool_latency_ms,
            record.verification_latency_ms,
            success,
            verified,
        )

    def get_recent(self, count: int = 10) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._history[-count:]]

    def clear(self) -> None:
        self._history.clear()


_telemetry_singleton: Optional[LatencyTelemetry] = None


def get_latency_telemetry() -> LatencyTelemetry:
    global _telemetry_singleton
    if _telemetry_singleton is None:
        _telemetry_singleton = LatencyTelemetry()
    return _telemetry_singleton
