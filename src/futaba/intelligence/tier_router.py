"""
Futaba Tier Router — Three Execution Speed Tiers.

Enforces the explicit execution speed hierarchy:
- TIER 0: Direct Action (~100ms - 2s). 0 planner calls, direct handler + verification.
- TIER 1: Fast Interactive Task (~0.5s - 5s). Semantic UI targeting (UIA/DOM) + micro-action + verify.
- TIER 2: Complex Autonomous Task (Dynamic). Hermes AgentController multi-step planning.

Enforces minimum latency consistent with reliable execution.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Optional

from futaba.intelligence.intent_resolver import IntentCategory, ResolvedIntent
from futaba.intelligence.micro_action import get_micro_action_engine, MicroActionResult
from futaba.intelligence.context_engine import get_context_engine

logger = logging.getLogger("futaba.intelligence.tier_router")


class ExecutionTier(str, enum.Enum):
    TIER_0 = "TIER_0"  # Direct Action: deterministic, <2s, no planner
    TIER_1 = "TIER_1"  # Fast Interactive Task: semantic UI targeting, 0.5-5s, no planner
    TIER_2 = "TIER_2"  # Complex Autonomous Task: multi-step Hermes planner


@dataclass
class TierRouteDecision:
    """The routing decision for a given user instruction."""
    tier: ExecutionTier
    action: str
    target: Any = None
    reason: str = ""
    is_direct: bool = True
    needs_planner: bool = False


class TierRouter:
    """Classifies requests into the minimum-cost speed tier."""

    # Interactive targeting keywords that indicate Tier 1 (semantic UI resolution needed)
    _TIER_1_PATTERNS = [
        "third short", "first video", "second option", "general channel",
        "download button", "settings button", "first result", "search box",
        "second result", "third result", "next video", "previous video",
        "play the first", "click the", "open the first", "open the second",
        "open the third", "select the",
    ]

    # Deterministic commands that are Tier 0
    _TIER_0_ACTIONS = {
        "focus", "launch", "close", "scroll", "keypress",
        "play", "pause", "navigate", "hotkey", "go_back",
    }

    def classify_tier(self, intent: ResolvedIntent, raw_text: str) -> TierRouteDecision:
        """
        Determine the lowest-cost execution tier capable of solving the task.
        """
        lower = raw_text.lower().strip()
        cat = intent.category

        # 1. Compound or complex multi-step tasks -> Tier 2
        compound_indicators = [
            " and ", " then ", "after that", "compare", "install",
            "download and", "find the cheapest", "recreate", "verify that",
            "download the latest", "configure the",
        ]
        if any(ci in lower for ci in compound_indicators):
            return TierRouteDecision(
                tier=ExecutionTier.TIER_2,
                action="autonomous_plan",
                target=raw_text,
                reason="Complex multi-step workflow requiring exploration or dependency chaining",
                is_direct=False,
                needs_planner=True,
            )

        # 2. Fast Interactive Tasks -> Tier 1 (Semantic UI targeting without planner)
        # Matches queries like "click the third Short", "open the first video", "open Donut SMP", "go to general"
        if (
            any(p in lower for p in self._TIER_1_PATTERNS)
            or cat in (IntentCategory.APPLICATION_NAVIGATION, IntentCategory.BROWSER_INTERACTION)
            or (cat == IntentCategory.TASK_EXECUTION and not any(ci in lower for ci in compound_indicators))
            or any(lower.startswith(act) for act in ("click ", "select ", "type ", "press "))
        ):
            target = intent.target_entity or intent.parameters.get("query") or intent.parameters.get("target") or raw_text
            action = intent.action or "click"
            return TierRouteDecision(
                tier=ExecutionTier.TIER_1,
                action=action,
                target=target,
                reason="Semantic UI interaction resolvable via UIA/DOM without multi-step planner",
                is_direct=True,
                needs_planner=False,
            )

        # 3. Direct application launcher / focus / close -> Tier 0
        if cat in (IntentCategory.APPLICATION_LAUNCH, IntentCategory.APPLICATION_FOCUS):
            return TierRouteDecision(
                tier=ExecutionTier.TIER_0,
                action=intent.action or "open",
                target=intent.target_application,
                reason="Deterministic application lifecycle control",
                is_direct=True,
                needs_planner=False,
            )

        # 4. Simple browser navigation to known service or URL -> Tier 0
        if cat == IntentCategory.BROWSER_NAVIGATION and not any(kw in lower for kw in ("and ", "then ", "search ")):
            return TierRouteDecision(
                tier=ExecutionTier.TIER_0,
                action="navigate",
                target=intent.parameters.get("url") or intent.target_entity,
                reason="Direct URL navigation",
                is_direct=True,
                needs_planner=False,
            )

        # 5. Media control (pause, resume, play, volume) -> Tier 0
        if cat == IntentCategory.TASK_CONTROL or lower in ("pause", "resume", "play", "stop", "mute", "unmute", "turn volume down", "turn volume up"):
            return TierRouteDecision(
                tier=ExecutionTier.TIER_0,
                action="play" if "play" in lower or "resume" in lower else "pause",
                target="media",
                reason="Deterministic media control",
                is_direct=True,
                needs_planner=False,
            )

        # 6. Voice-only / conversation -> Tier 0
        if cat in (IntentCategory.CONVERSATION, IntentCategory.SLEEP_COMMAND, IntentCategory.INFORMATION_QUERY):
            return TierRouteDecision(
                tier=ExecutionTier.TIER_0,
                action="voice_response",
                reason="Conversational query requires no UI manipulation",
                is_direct=True,
                needs_planner=False,
            )

        # Default fallback to Tier 1 for simple user commands
        return TierRouteDecision(
            tier=ExecutionTier.TIER_1,
            action="interact",
            target=raw_text,
            reason="Interactive UI action",
            is_direct=True,
            needs_planner=False,
        )

    async def execute_fast_path(self, decision: TierRouteDecision, context: Any = None) -> MicroActionResult:
        """
        Execute Tier 0 or Tier 1 action via MicroActionEngine directly.
        """
        engine = get_micro_action_engine()
        return await engine.execute_micro_action(
            action=decision.action,
            target=decision.target,
            context=context,
        )


_tier_router: Optional[TierRouter] = None


def get_tier_router() -> TierRouter:
    global _tier_router
    if _tier_router is None:
        _tier_router = TierRouter()
    return _tier_router
