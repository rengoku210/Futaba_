"""
Futaba core configuration system.

Hierarchical configuration with:
- Default values
- User config file (YAML)
- Environment variable overrides
- Runtime overrides

Configuration is validated through Pydantic models and stored at:
  %LOCALAPPDATA%\\Futaba\\config\\futaba.yaml
"""

from __future__ import annotations

import os
import sys
import enum
import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator

logger = logging.getLogger("futaba.core.config")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_app_data_dir() -> Path:
    """Return the Futaba application data directory."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Futaba"


def get_config_path() -> Path:
    return get_app_data_dir() / "config" / "futaba.yaml"


def get_runtime_dir() -> Path:
    return get_app_data_dir() / "runtime"


def get_logs_dir() -> Path:
    return get_app_data_dir() / "logs"


def get_tasks_dir() -> Path:
    if "FUTABA_TASKS_DIR" in os.environ:
        return Path(os.environ["FUTABA_TASKS_DIR"])
    if os.environ.get("FUTABA_ENV") == "test" or "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST")):
        return get_app_data_dir() / "test_tasks"
    return get_app_data_dir() / "tasks"


def get_quarantine_dir() -> Path:
    if "FUTABA_QUARANTINE_DIR" in os.environ:
        return Path(os.environ["FUTABA_QUARANTINE_DIR"])
    return get_app_data_dir() / "quarantine"


def get_memory_dir() -> Path:
    return get_app_data_dir() / "memory"


def get_cache_dir() -> Path:
    return get_app_data_dir() / "cache"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AutonomyLevel(int, enum.Enum):
    """How autonomous Futaba should be."""
    ASK_EVERYTHING = 0
    SAFE_AUTO = 1       # Execute safe actions automatically
    MOSTLY_AUTO = 2     # Execute most actions automatically
    HIGHLY_AUTO = 3     # Highly autonomous
    MAX_AUTO = 4        # Maximum autonomy within boundaries


class DestructiveActionPolicy(str, enum.Enum):
    ALWAYS_ASK = "always_ask"
    ASK_IMPORTANT = "ask_important"
    ALLOW_ALL = "allow_all"


class NotificationStyle(str, enum.Enum):
    MINIMAL = "minimal"
    NORMAL = "normal"
    VERBOSE = "verbose"
    SILENT = "silent"


# ---------------------------------------------------------------------------
# Configuration Models
# ---------------------------------------------------------------------------

class GeneralConfig(BaseModel):
    """General application settings."""
    name: str = "Futaba"
    wake_phrase: str = "Hey Futaba"
    language: str = "en"
    launch_on_startup: bool = False
    minimize_to_tray: bool = True
    notification_style: NotificationStyle = NotificationStyle.NORMAL


class ProviderConfig(BaseModel):
    """A single AI provider configuration."""
    name: str
    kind: str = "openai_compatible"  # openai_compatible, openrouter, ollama, lm_studio
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    default_model: str = ""
    max_tokens: int = 4096
    timeout_seconds: int = 120
    enabled: bool = True

    class Config:
        json_encoders = {SecretStr: lambda v: "***" if v else ""}


def _default_providers() -> list[ProviderConfig]:
    return [
        ProviderConfig(
            name="openrouter",
            kind="openrouter",
            base_url="https://openrouter.ai/api/v1",
            default_model="google/gemini-2.5-flash",
        ),
        ProviderConfig(
            name="gemini",
            kind="openai_compatible",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-3.6-flash",
        ),
        ProviderConfig(
            name="ollama",
            kind="ollama",
            base_url="http://localhost:11434/v1",
            default_model="llama3.2:latest",
        ),
        ProviderConfig(
            name="openai",
            kind="openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
        ),
    ]


class ModelRoutingConfig(BaseModel):
    """Model routing rules."""
    planner_provider: str = "openrouter"
    planner_model: str = "google/gemini-2.5-flash"
    executor_provider: str = "openrouter"
    executor_model: str = "google/gemini-2.5-flash"
    vision_provider: str = "openrouter"
    vision_model: str = "google/gemini-2.5-flash"
    fallback_provider: str = "gemini"
    fallback_model: str = "gemini-3.6-flash"
    fast_provider: str = "openrouter"
    fast_model: str = "google/gemini-2.5-flash"
    # If empty strings, uses the default provider/model
    auto_route: bool = True
    cost_aware: bool = True
    latency_aware: bool = True


class AIConfig(BaseModel):
    """AI / LLM settings."""
    default_provider: str = "openrouter"
    providers: list[ProviderConfig] = Field(default_factory=_default_providers)
    routing: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    temperature: float = 0.7
    max_context_tokens: int = 128000
    stream: bool = True


class AutonomyConfig(BaseModel):
    """Autonomy and execution policy."""
    level: AutonomyLevel = AutonomyLevel.MOSTLY_AUTO
    confirmation_required_for: list[str] = Field(
        default_factory=lambda: ["delete_files", "install_software", "system_settings"]
    )
    max_task_duration_minutes: int = 120
    max_retries: int = 5
    blocker_timeout_seconds: int = 300
    background_execution: bool = True
    destructive_action_policy: DestructiveActionPolicy = DestructiveActionPolicy.ASK_IMPORTANT


class VoiceConfig(BaseModel):
    """Voice and wake-word settings."""
    enabled: bool = True
    provider: str = "gemini_live"  # gemini_live, local, disabled
    wake_word_enabled: bool = True
    wake_word: str = "hey futaba"
    microphone_device: str = ""  # Empty = system default
    speaker_device: str = ""     # Empty = system default
    preferred_language: str = "en"
    stt_provider: str = "local"  # local, openai, google
    stt_model: str = "base"     # whisper model size for local
    tts_enabled: bool = True
    tts_provider: str = "local"
    tts_voice: str = ""
    volume: float = 0.8
    activation_sensitivity: float = 0.5

    # Gemini Live specific settings
    gemini_live_model: str = "gemini-3.8-live"
    gemini_live_voice: str = "Aoede"  # Female: Aoede, Kore | Male: Puck, Charon, Fenrir
    gemini_live_auto_reconnect: bool = True
    gemini_live_temperature: float = 0.6
    personality: str = "Futaba — Sarcastic Scientist"
    personality_intensity: str = "medium"  # low, medium, high
    personality_system_prompt: str = ""  # Custom system prompt override if provided
    engagement_silence_timeout: float = 45.0  # Seconds of silence before returning to DORMANT


class ComputerUseConfig(BaseModel):
    """Computer-use / CUA settings."""
    enabled: bool = True
    background_mode: bool = True
    permission_mode: str = "ask_elevated"  # allow_all, ask_elevated, restricted
    app_restrictions: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
    screenshot_interval_ms: int = 1000


class PerformanceConfig(BaseModel):
    """Resource management settings."""
    cpu_limit_percent: float = 50.0
    gpu_preference: str = "auto"  # auto, cuda, cpu
    ram_budget_mb: int = 2048
    background_priority: str = "below_normal"  # low, below_normal, normal
    gaming_mode_enabled: bool = True
    idle_behavior: str = "sleep"  # sleep, low_poll, active


class AppearanceConfig(BaseModel):
    """Appearance settings."""
    theme: str = "dark"
    accent_color: str = "#7B2FBE"  # Futaba purple
    hud_size: int = 64
    hud_opacity: float = 0.85
    animations_enabled: bool = True


class MemoryConfig(BaseModel):
    """Memory system settings."""
    enabled: bool = True
    max_entries: int = 10000
    retention_days: int = 365
    store_task_history: bool = True
    store_user_preferences: bool = True
    store_project_context: bool = True


class HermesConfig(BaseModel):
    """Configuration for the underlying Hermes Agent integration."""
    hermes_dir: str = ""  # Auto-detected
    config_override: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    tools_enabled: list[str] = Field(
        default_factory=lambda: [
            "computer_use", "browser", "terminal", "filesystem", "mcp"
        ]
    )


class FutabaConfig(BaseModel):
    """Root configuration for Futaba."""
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    appearance: AppearanceConfig = Field(default_factory=AppearanceConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    system_prompt: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "FutabaConfig":
        """Load configuration from YAML file, falling back to defaults."""
        path = path or get_config_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                return cls.model_validate(data)
            except Exception as e:
                logger.warning("Failed to load config from %s: %s. Using defaults.", path, e)
        return cls()

    def save(self, path: Path | None = None) -> None:
        """Save configuration to YAML file."""
        path = path or get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Convert to dict, masking secrets
        data = self.model_dump(mode="json")
        # Don't persist secret values in plaintext — they go through
        # the security module's credential store
        self._strip_secrets(data)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    def _strip_secrets(self, d: dict) -> None:
        """Recursively remove SecretStr values from a dict for safe serialization."""
        for key, val in list(d.items()):
            if isinstance(val, dict):
                self._strip_secrets(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        self._strip_secrets(item)
            # SecretStr serializes to the raw value in model_dump(mode="json")
            # We replace api_key fields with empty string
            if key == "api_key" and isinstance(val, str) and val:
                d[key] = ""  # Will be loaded from credential store


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_config: FutabaConfig | None = None


def get_config() -> FutabaConfig:
    """Get the global Futaba configuration (lazy-loaded singleton)."""
    global _config
    if _config is None:
        _config = FutabaConfig.load()
    return _config


def reload_config() -> FutabaConfig:
    """Force reload configuration from disk."""
    global _config
    _config = FutabaConfig.load()
    return _config


def update_config(updates: dict[str, Any]) -> FutabaConfig:
    """Apply partial updates to the configuration and save."""
    global _config
    cfg = get_config()
    data = cfg.model_dump(mode="json")
    _deep_merge(data, updates)
    _config = FutabaConfig.model_validate(data)
    _config.save()
    return _config


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
