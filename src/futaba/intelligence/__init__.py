"""
Futaba Intelligence — Cognitive architecture for context-aware Windows AI copilot.

This package provides:
- ContextEngine: Continuous system state model (active app, browser, task, conversation)
- IntentResolver: Application-aware intent classification
- EntityResolver: Ambiguous entity resolution against context
- ExecutionSurfaceSelector: Execution method routing
- FutabaPersonality: Personality response layer
"""

from futaba.intelligence.context_engine import (
    ContextEngine,
    ApplicationContext,
    BrowserState,
    ContextState,
    get_context_engine,
)
from futaba.intelligence.intent_resolver import (
    IntentResolver,
    IntentCategory,
    ResolvedIntent,
    get_intent_resolver,
)
from futaba.intelligence.entity_resolver import (
    EntityResolver,
    ResolvedEntity,
    get_entity_resolver,
)
from futaba.intelligence.execution_surface import (
    ExecutionSurface,
    ExecutionSurfaceSelector,
    get_surface_selector,
)
from futaba.intelligence.personality import (
    FutabaPersonality,
    get_personality,
)
from futaba.intelligence.telemetry import (
    LatencyRecord,
    LatencyTelemetry,
    get_latency_telemetry,
)
from futaba.intelligence.semantic_ui import (
    SemanticUIElement,
    SemanticUITargeter,
    get_semantic_targeter,
)
from futaba.intelligence.micro_action import (
    MicroActionResult,
    MicroActionEngine,
    get_micro_action_engine,
)
from futaba.intelligence.tier_router import (
    ExecutionTier,
    TierRouteDecision,
    TierRouter,
    get_tier_router,
)

__all__ = [
    "ContextEngine",
    "ApplicationContext",
    "BrowserState",
    "ContextState",
    "get_context_engine",
    "IntentResolver",
    "IntentCategory",
    "ResolvedIntent",
    "get_intent_resolver",
    "EntityResolver",
    "ResolvedEntity",
    "get_entity_resolver",
    "ExecutionSurface",
    "ExecutionSurfaceSelector",
    "get_surface_selector",
    "FutabaPersonality",
    "get_personality",
    "LatencyRecord",
    "LatencyTelemetry",
    "get_latency_telemetry",
    "SemanticUIElement",
    "SemanticUITargeter",
    "get_semantic_targeter",
    "MicroActionResult",
    "MicroActionEngine",
    "get_micro_action_engine",
    "ExecutionTier",
    "TierRouteDecision",
    "TierRouter",
    "get_tier_router",
]
