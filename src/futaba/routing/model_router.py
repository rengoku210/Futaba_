"""
Futaba Model Router — Intelligent model selection and provider management.

The router selects the best model for each task based on:
- Task complexity (simple command vs. complex reasoning)
- Required capabilities (vision, coding, planning)
- Resource constraints (local GPU load, RAM, battery)
- Cost/latency preferences
- Provider availability
- Fallback chains

Architecture:
    TaskExecutor → ModelRouter.select(task_context) → ProviderClient
                                                         ↓
                                                    LLM API call

The router wraps provider-specific clients behind a uniform interface
so the rest of Futaba never deals with provider details directly.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from futaba.core.config import (
    get_config,
    AIConfig,
    ProviderConfig,
    ModelRoutingConfig,
)

logger = logging.getLogger("futaba.routing")


# ---------------------------------------------------------------------------
# Task Context — what the router uses to decide
# ---------------------------------------------------------------------------

class TaskComplexity(str, enum.Enum):
    """How complex the task is — affects model selection."""
    TRIVIAL = "trivial"       # "Open Chrome"
    SIMPLE = "simple"         # "List files in Downloads"
    MODERATE = "moderate"     # "Find and fix the bug"
    COMPLEX = "complex"       # "Build a website from this design"
    EXPERT = "expert"         # Complex multi-step planning


class TaskCapability(str, enum.Enum):
    """Required model capabilities."""
    TEXT = "text"
    CODING = "coding"
    REASONING = "reasoning"
    VISION = "vision"
    PLANNING = "planning"
    FAST = "fast"             # Low-latency required


@dataclass
class ModelRequest:
    """
    Context for model selection. Created by the agent controller
    before each LLM call.
    """
    role: str = "executor"          # planner, executor, verifier, vision, fast
    complexity: TaskComplexity = TaskComplexity.MODERATE
    capabilities: list[TaskCapability] = field(default_factory=lambda: [TaskCapability.TEXT])
    prefer_local: bool = False       # Prefer local model if available
    max_latency_ms: int = 0          # 0 = no constraint
    max_cost_per_token: float = 0.0  # 0 = no constraint
    context_tokens_needed: int = 4096
    task_id: str = ""                # For logging/tracking


# ---------------------------------------------------------------------------
# Unified Message Format
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    """A single message in a conversation."""
    role: str  # system, user, assistant, tool
    content: str | list[dict[str, Any]] = ""  # Text or multimodal content blocks
    name: str = ""
    tool_call_id: str = ""
    tool_calls: list[dict[str, Any]] | None = None

    def to_openai_dict(self) -> dict[str, Any]:
        """Convert to OpenAI API format."""
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d


@dataclass
class CompletionResponse:
    """Unified response from any provider."""
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    usage: dict[str, Any] = field(default_factory=dict)  # prompt_tokens, completion_tokens, reasoning_tokens
    latency_ms: float = 0.0
    finish_reason: str = ""
    raw: Any = None  # Provider-specific raw response


@dataclass
class StreamChunk:
    """A single chunk in a streaming response."""
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider Client Interface
# ---------------------------------------------------------------------------

class ProviderClient(ABC):
    """Abstract interface for LLM provider clients."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        self._healthy = True
        self._last_error: str = ""
        self._last_success: float = 0.0
        self._consecutive_failures: int = 0

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict = "auto",
        **kwargs: Any,
    ) -> CompletionResponse:
        """Send a completion request."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[ChatMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict = "auto",
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Send a streaming completion request."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the provider is reachable and functional."""
        ...

    def record_success(self) -> None:
        self._healthy = True
        self._last_success = time.time()
        self._consecutive_failures = 0

    def record_failure(self, error: str) -> None:
        self._consecutive_failures += 1
        self._last_error = error
        if self._consecutive_failures >= 3:
            self._healthy = False
            logger.warning(
                "Provider %s marked unhealthy after %d consecutive failures: %s",
                self.name, self._consecutive_failures, error
            )


# ---------------------------------------------------------------------------
# OpenAI-Compatible Provider Client
# ---------------------------------------------------------------------------

class OpenAICompatibleClient(ProviderClient):
    """
    Client for any OpenAI-compatible API.

    Works with: OpenAI, OpenRouter, Ollama, LM Studio, vLLM,
    and any endpoint following the OpenAI chat completions spec.
    """

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        api_key = config.api_key.get_secret_value() if config.api_key else ""
        if not api_key or api_key == "no-key":
            try:
                from futaba.security.credentials import CredentialStore
                api_key = CredentialStore().get(f"provider:{config.name}") or ""
            except Exception:
                pass
        if not api_key:
            import os
            env_var = f"{config.name.upper()}_API_KEY"
            api_key = os.environ.get(env_var, "") or os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key and config.kind == "ollama":
            api_key = "ollama"
        api_key = api_key or "no-key"

        base_url = config.base_url or "https://openrouter.ai/api/v1"
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
        )
        self._default_model = config.default_model

    async def complete(
        self,
        messages: list[ChatMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict = "auto",
        **kwargs: Any,
    ) -> CompletionResponse:
        model = model or self._default_model
        start = time.monotonic()

        api_messages = [m.to_openai_dict() for m in messages]
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = tool_choice
        call_kwargs.update(kwargs)

        try:
            response = await self._client.chat.completions.create(**call_kwargs)
            elapsed = (time.monotonic() - start) * 1000

            choice = response.choices[0] if response.choices else None
            tool_calls_out = []
            if choice and choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    tool_calls_out.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    })

            reasoning_text = ""
            if choice and hasattr(choice.message, "reasoning"):
                reasoning_text = str(getattr(choice.message, "reasoning", "") or "")
            elif choice and hasattr(choice.message, "reasoning_content"):
                reasoning_text = str(getattr(choice.message, "reasoning_content", "") or "")

            usage_dict = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
            }
            if response.usage:
                details = getattr(response.usage, "completion_tokens_details", None)
                if details:
                    r_toks = getattr(details, "reasoning_tokens", None)
                    if r_toks is not None:
                        usage_dict["reasoning_tokens"] = r_toks

            result = CompletionResponse(
                content=choice.message.content or "" if choice else "",
                reasoning=reasoning_text,
                tool_calls=tool_calls_out,
                model=response.model or model,
                provider=self.name,
                usage=usage_dict,
                latency_ms=elapsed,
                finish_reason=choice.finish_reason or "" if choice else "",
                raw=response,
            )
            self.record_success()
            return result

        except Exception as e:
            self.record_failure(str(e))
            raise

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict = "auto",
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        model = model or self._default_model
        api_messages = [m.to_openai_dict() for m in messages]

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = tool_choice
        call_kwargs.update(kwargs)

        try:
            stream = await self._client.chat.completions.create(**call_kwargs)
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta if choice else None

                chunk_usage = {}
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    chunk_usage = {
                        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                    }
                    details = getattr(u, "completion_tokens_details", None)
                    if details:
                        r_toks = getattr(details, "reasoning_tokens", None)
                        if r_toks is not None:
                            chunk_usage["reasoning_tokens"] = r_toks

                if not delta and not chunk_usage:
                    continue

                tc_list = []
                reasoning_delta = ""
                if delta:
                    reasoning_delta = (
                        str(getattr(delta, "reasoning", "") or "")
                        or str(getattr(delta, "reasoning_content", "") or "")
                    )
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            tc_list.append({
                                "index": tc.index,
                                "id": getattr(tc, "id", None),
                                "type": getattr(tc, "type", None),
                                "function": {
                                    "name": getattr(tc.function, "name", None) if tc.function else None,
                                    "arguments": getattr(tc.function, "arguments", "") if tc.function else "",
                                }
                            })

                yield StreamChunk(
                    content=delta.content or "" if delta else "",
                    reasoning=reasoning_delta,
                    tool_calls=tc_list,
                    finish_reason=choice.finish_reason or "" if choice else "",
                    model=chunk.model or model,
                    usage=chunk_usage,
                )
            self.record_success()

        except Exception as e:
            self.record_failure(str(e))
            raise

    async def health_check(self) -> bool:
        """Quick health check — sends a minimal completion request."""
        try:
            response = await self._client.chat.completions.create(
                model=self._default_model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            self.record_success()
            return True
        except Exception as e:
            self.record_failure(str(e))
            return False


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """Manages provider clients and their lifecycle."""

    def __init__(self):
        self._clients: dict[str, ProviderClient] = {}

    def register(self, config: ProviderConfig) -> ProviderClient:
        """Register a provider from its configuration."""
        if config.kind in ("openai_compatible", "openrouter", "openai", "ollama", "lm_studio"):
            client = OpenAICompatibleClient(config)
        else:
            logger.warning("Unknown provider kind '%s', using OpenAI-compatible", config.kind)
            client = OpenAICompatibleClient(config)

        self._clients[config.name] = client
        logger.info("Registered provider: %s (%s)", config.name, config.kind)
        return client

    def get(self, name: str) -> ProviderClient | None:
        return self._clients.get(name)

    def get_healthy(self) -> list[ProviderClient]:
        return [c for c in self._clients.values() if c.is_healthy]

    def all(self) -> list[ProviderClient]:
        return list(self._clients.values())

    @classmethod
    def from_config(cls, ai_config: AIConfig) -> "ProviderRegistry":
        """Build registry from the AI configuration."""
        registry = cls()
        for provider_cfg in ai_config.providers:
            if provider_cfg.enabled:
                registry.register(provider_cfg)

        if not registry.all():
            from futaba.core.config import _default_providers
            for provider_cfg in _default_providers():
                registry.register(provider_cfg)

        return registry


# ---------------------------------------------------------------------------
# Model Router
# ---------------------------------------------------------------------------

class ModelRouter:
    """
    Intelligent model routing.

    Given a ModelRequest describing what capabilities are needed,
    the router selects the best (provider, model) combination based on:

    1. Explicit routing config (planner_model, executor_model, etc.)
    2. Capability matching
    3. Provider health
    4. Complexity-based selection
    5. Fallback chains

    The router is the single point of contact for all LLM calls in Futaba.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        routing_config: ModelRoutingConfig | None = None,
        default_provider: str = "",
    ):
        self._registry = registry
        self._routing = routing_config or ModelRoutingConfig()
        self._default_provider = default_provider
        self._call_count = 0
        self._total_tokens = 0

    def select(self, request: ModelRequest) -> tuple[ProviderClient, str]:
        """
        Select the best (provider, model) for a request.

        Returns:
            (ProviderClient, model_name)

        Raises:
            RuntimeError if no suitable provider is available.
        """
        # 1. Try explicit routing first
        provider_name, model = self._route_explicit(request)
        if provider_name and model:
            client = self._registry.get(provider_name)
            if client and client.is_healthy:
                logger.debug(
                    "Routed %s/%s to %s/%s (explicit)",
                    request.role, request.complexity.value,
                    provider_name, model
                )
                return client, model

        # 2. Try default provider
        default = self._registry.get(self._default_provider)
        if default and default.is_healthy:
            model = self._select_model_for_complexity(request, default)
            return default, model

        # 3. Try any healthy provider
        healthy = self._registry.get_healthy()
        if healthy:
            client = healthy[0]
            model = client.config.default_model
            logger.warning(
                "Falling back to provider %s for %s request",
                client.name, request.role
            )
            return client, model

        raise RuntimeError(
            "No healthy AI providers available. "
            "Check provider configuration and network connectivity."
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        request: ModelRequest | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """
        High-level completion with automatic routing, retry, and exhaustive
        fallback across ALL registered providers.
        """
        request = request or ModelRequest()
        config = get_config()
        temp = temperature if temperature is not None else config.ai.temperature
        max_tok = max_tokens or 8192

        # Try primary selection
        client, model = self.select(request)

        errors: list[str] = []

        try:
            response = await client.complete(
                messages=messages,
                model=model,
                temperature=temp,
                max_tokens=max_tok,
                tools=tools,
                **kwargs,
            )
            self._call_count += 1
            self._total_tokens += sum(response.usage.values())
            return response

        except Exception as primary_err:
            logger.warning(
                "Primary provider %s/%s failed: %s. Trying fallbacks.",
                client.name, model, primary_err
            )
            errors.append(f"{client.name}/{model}: {primary_err}")
            client.record_failure(str(primary_err))

        # Exhaustive fallback: try ALL other healthy providers
        for fallback in self._registry.all():
            if fallback is client:
                continue  # Skip the failed primary
            if not fallback.is_healthy:
                continue
            try:
                fb_model = fallback.config.default_model
                logger.info(
                    "Attempting fallback provider %s/%s for %s request",
                    fallback.name, fb_model, request.role
                )
                response = await fallback.complete(
                    messages=messages,
                    model=fb_model,
                    temperature=temp,
                    max_tokens=max_tok,
                    tools=tools,
                    **kwargs,
                )
                self._call_count += 1
                self._total_tokens += sum(response.usage.values())
                logger.info("Fallback provider %s succeeded.", fallback.name)
                return response
            except Exception as fallback_err:
                logger.warning(
                    "Fallback provider %s failed: %s", fallback.name, fallback_err
                )
                errors.append(f"{fallback.name}: {fallback_err}")
                fallback.record_failure(str(fallback_err))

        error_summary = "; ".join(errors)
        raise RuntimeError(
            f"All {len(errors)} providers failed. Details: {error_summary}"
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        request: ModelRequest | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion with automatic routing."""
        request = request or ModelRequest()
        config = get_config()
        temp = temperature if temperature is not None else config.ai.temperature

        client, model = self.select(request)
        async for chunk in client.stream(
            messages=messages,
            model=model,
            temperature=temp,
            max_tokens=max_tokens or 4096,
            tools=tools,
            **kwargs,
        ):
            yield chunk

    def _route_explicit(self, request: ModelRequest) -> tuple[str, str]:
        """Check if there's an explicit routing rule for this request role."""
        r = self._routing
        mapping = {
            "planner": (r.planner_provider, r.planner_model),
            "executor": (r.executor_provider, r.executor_model),
            "vision": (r.vision_provider, r.vision_model),
            "fast": (r.fast_provider, r.fast_model),
        }
        provider, model = mapping.get(request.role, ("", ""))
        if provider and model:
            return provider, model

        # Fallback mapping if no explicit role match
        if request.role == "verifier":
            return r.executor_provider, r.executor_model

        return "", ""

    def _select_model_for_complexity(
        self,
        request: ModelRequest,
        client: ProviderClient,
    ) -> str:
        """Select model based on task complexity when no explicit routing exists."""
        # For now, always use the default model. The routing config
        # can specify different models per role for more control.
        return client.config.default_model

    def _get_fallback_client(self) -> ProviderClient | None:
        """Get the configured fallback provider."""
        if self._routing.fallback_provider:
            client = self._registry.get(self._routing.fallback_provider)
            if client and client.is_healthy:
                return client

        # Try any other healthy provider
        healthy = self._registry.get_healthy()
        return healthy[0] if healthy else None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
            "providers": {
                c.name: {
                    "healthy": c.is_healthy,
                    "last_error": c._last_error,
                }
                for c in self._registry.all()
            },
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_router() -> ModelRouter:
    """Create a ModelRouter from the current configuration."""
    config = get_config()
    registry = ProviderRegistry.from_config(config.ai)
    return ModelRouter(
        registry=registry,
        routing_config=config.ai.routing,
        default_provider=config.ai.default_provider,
    )
