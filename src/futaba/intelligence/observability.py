"""
Futaba Structured Command Observability.

Implements high-fidelity structured audit logging for every command execution:
USER_UTTERANCE
INTENT
ENTITY
CURRENT_APP
CURRENT_WINDOW
CONTEXT
EXECUTION_SURFACE
ACTION
EXPECTED_RESULT
OBSERVED_RESULT
VERIFICATION
LATENCY
FINAL_RESULT
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Optional

from futaba.core.config import get_logs_dir

logger = logging.getLogger("futaba.observability")


@dataclass
class CommandAuditRecord:
    user_utterance: str
    intent: str
    entity: str
    current_app: str = "Unknown"
    current_window: str = "Unknown"
    context: str = ""
    execution_surface: str = "DIRECT"
    action: str = ""
    expected_result: str = ""
    observed_result: str = ""
    verification: str = "PENDING"  # PASS | FAIL | SKIPPED
    latency_ms: float = 0.0
    final_result: str = "PENDING"  # SUCCESS | FAILURE
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def format_log(self) -> str:
        """Format as human-readable structured block for logs."""
        lines = [
            "=" * 70,
            f"USER: \"{self.user_utterance}\"",
            "",
            f"INTENT: {self.intent}",
            f"ENTITY: {self.entity}",
            f"CURRENT_APP: {self.current_app}",
            f"CURRENT_WINDOW: {self.current_window}",
            f"CONTEXT: {self.context}" if self.context else "CONTEXT: -",
            f"EXECUTION_SURFACE: {self.execution_surface}",
            f"ACTION: {self.action}",
            f"EXPECTED: {self.expected_result}",
            f"OBSERVED: {self.observed_result}",
            f"VERIFICATION: {self.verification}",
            f"LATENCY: {self.latency_ms:.1f}ms",
            f"RESULT: {self.final_result}",
            "=" * 70,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommandObserver:
    """Singleton auditor recording all executed commands to file and logger."""

    def __init__(self) -> None:
        self._history: list[CommandAuditRecord] = []
        self._log_file: Optional[Path] = None

    def _get_log_file(self) -> Path:
        if self._log_file is None:
            log_dir = get_logs_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = log_dir / "commands.log"
        return self._log_file

    def record(self, record: CommandAuditRecord) -> None:
        """Record and persist an audit record."""
        self._history.append(record)
        formatted = record.format_log()
        logger.info("\n%s", formatted)

        # Append to commands.log
        try:
            with open(self._get_log_file(), "a", encoding="utf-8") as f:
                f.write(formatted + "\n\n")
        except Exception as e:
            logger.debug("Failed to write command audit log: %s", e)

    def get_latest(self) -> Optional[CommandAuditRecord]:
        return self._history[-1] if self._history else None

    def get_recent(self, count: int = 10) -> list[CommandAuditRecord]:
        return self._history[-count:]


# Global singleton
_observer: Optional[CommandObserver] = None


def get_command_observer() -> CommandObserver:
    global _observer
    if _observer is None:
        _observer = CommandObserver()
    return _observer
