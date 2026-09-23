"""
Futaba Security — Credential management and security boundaries.

Uses the Windows Credential Manager for secure storage of:
- API keys
- Provider tokens
- Service passwords

Credentials are never:
- Stored in plain-text config files
- Logged
- Included in LLM context
- Exposed through the IPC API

Architecture:
    FutabaConfig (references credential names)
         ↓
    CredentialStore (reads/writes Windows Credential Manager)
         ↓
    Windows DPAPI-encrypted credential vault
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("futaba.security")

# Credential prefix to namespace Futaba credentials
CREDENTIAL_PREFIX = "Futaba:"

# ---------------------------------------------------------------------------
# Windows Credential Manager
# ---------------------------------------------------------------------------

try:
    import ctypes
    import ctypes.wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    # CREDENTIAL structure
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    class CREDENTIAL_ATTRIBUTE(ctypes.Structure):
        _fields_ = [
            ("Keyword", ctypes.c_wchar_p),
            ("Flags", ctypes.wintypes.DWORD),
            ("ValueSize", ctypes.wintypes.DWORD),
            ("Value", ctypes.POINTER(ctypes.c_byte)),
        ]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.wintypes.DWORD),
            ("Type", ctypes.wintypes.DWORD),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWritten", ctypes.wintypes.FILETIME),
            ("CredentialBlobSize", ctypes.wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", ctypes.wintypes.DWORD),
            ("AttributeCount", ctypes.wintypes.DWORD),
            ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTE)),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]

    PCREDENTIAL = ctypes.POINTER(CREDENTIAL)

    HAS_WIN_CRED = True
except Exception:
    HAS_WIN_CRED = False


class CredentialStore:
    """
    Secure credential storage using Windows Credential Manager.

    All credentials are stored with a "Futaba:" prefix and encrypted
    by Windows DPAPI (tied to the user profile).
    """

    def store(self, name: str, value: str, username: str = "futaba") -> bool:
        """Store a credential securely."""
        target = f"{CREDENTIAL_PREFIX}{name}"

        if HAS_WIN_CRED:
            return self._store_win(target, value, username)
        else:
            # Fallback: environment variable (less secure)
            logger.warning(
                "Windows Credential Manager unavailable. "
                "Storing credential in environment (less secure)."
            )
            os.environ[f"FUTABA_CRED_{name.upper().replace(' ', '_')}"] = value
            return True

    def get(self, name: str) -> str | None:
        """Retrieve a credential."""
        target = f"{CREDENTIAL_PREFIX}{name}"

        if HAS_WIN_CRED:
            return self._get_win(target)
        else:
            env_key = f"FUTABA_CRED_{name.upper().replace(' ', '_')}"
            return os.environ.get(env_key)

    def delete(self, name: str) -> bool:
        """Delete a credential."""
        target = f"{CREDENTIAL_PREFIX}{name}"

        if HAS_WIN_CRED:
            return self._delete_win(target)
        else:
            env_key = f"FUTABA_CRED_{name.upper().replace(' ', '_')}"
            os.environ.pop(env_key, None)
            return True

    def list_credentials(self) -> list[str]:
        """List all Futaba credential names."""
        if HAS_WIN_CRED:
            return self._list_win()
        return []

    def exists(self, name: str) -> bool:
        """Check if a credential exists."""
        return self.get(name) is not None

    # --- Windows Credential Manager Implementation ---

    def _store_win(self, target: str, value: str, username: str) -> bool:
        try:
            blob = value.encode("utf-16-le")
            blob_array = (ctypes.c_byte * len(blob))(*blob)

            cred = CREDENTIAL()
            cred.Flags = 0
            cred.Type = CRED_TYPE_GENERIC
            cred.TargetName = target
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = ctypes.cast(blob_array, ctypes.POINTER(ctypes.c_byte))
            cred.Persist = CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = username

            result = advapi32.CredWriteW(ctypes.byref(cred), 0)
            if result:
                logger.info("Stored credential: %s", target)
                return True
            else:
                err = ctypes.get_last_error()
                logger.error("Failed to store credential %s: Win32 error %d", target, err)
                return False
        except Exception as e:
            logger.error("Failed to store credential %s: %s", target, e)
            return False

    def _get_win(self, target: str) -> str | None:
        try:
            pcred = PCREDENTIAL()
            result = advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
            if result:
                cred = pcred.contents
                if cred.CredentialBlobSize > 0:
                    blob = (ctypes.c_byte * cred.CredentialBlobSize)()
                    ctypes.memmove(blob, cred.CredentialBlob, cred.CredentialBlobSize)
                    value = bytes(blob).decode("utf-16-le")
                    advapi32.CredFree(pcred)
                    return value
                advapi32.CredFree(pcred)
            return None
        except Exception as e:
            logger.debug("Failed to read credential %s: %s", target, e)
            return None

    def _delete_win(self, target: str) -> bool:
        try:
            result = advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
            if result:
                logger.info("Deleted credential: %s", target)
                return True
            return False
        except Exception as e:
            logger.error("Failed to delete credential %s: %s", target, e)
            return False

    def _list_win(self) -> list[str]:
        try:
            count = ctypes.wintypes.DWORD()
            pcreds = ctypes.POINTER(PCREDENTIAL)()

            filter_str = f"{CREDENTIAL_PREFIX}*"
            result = advapi32.CredEnumerateW(
                filter_str, 0, ctypes.byref(count), ctypes.byref(pcreds)
            )

            names = []
            if result:
                for i in range(count.value):
                    cred = pcreds[i].contents
                    name = cred.TargetName
                    if name and name.startswith(CREDENTIAL_PREFIX):
                        names.append(name[len(CREDENTIAL_PREFIX):])
                advapi32.CredFree(pcreds)
            return names
        except Exception as e:
            logger.debug("Failed to list credentials: %s", e)
            return []


# ---------------------------------------------------------------------------
# Autonomy Policy Checker
# ---------------------------------------------------------------------------

class AutonomyChecker:
    """
    Checks whether an action is permitted under the current autonomy policy.

    This is called before potentially dangerous operations:
    - File deletion
    - Software installation
    - System settings changes
    - Elevated operations
    """

    def __init__(self, config: Any = None):
        from futaba.core.config import get_config, AutonomyLevel
        self._config = config or get_config()
        self._level = self._config.autonomy.level

    def is_allowed(self, action: str, is_destructive: bool = False) -> bool:
        """Check if an action is allowed without user confirmation."""
        from futaba.core.config import AutonomyLevel

        if self._level == AutonomyLevel.ASK_EVERYTHING:
            return False

        if self._level == AutonomyLevel.MAX_AUTO:
            return True

        if is_destructive:
            if self._level <= AutonomyLevel.SAFE_AUTO:
                return False
            if action in self._config.autonomy.confirmation_required_for:
                return self._level >= AutonomyLevel.HIGHLY_AUTO

        # Non-destructive actions at level >= SAFE_AUTO
        return self._level >= AutonomyLevel.SAFE_AUTO

    def requires_confirmation(self, action: str, context: str = "") -> bool:
        """Check if an action requires explicit user confirmation."""
        return not self.is_allowed(action, is_destructive=True)


# ---------------------------------------------------------------------------
# Log Sanitizer
# ---------------------------------------------------------------------------

class LogSanitizer:
    """Ensure secrets never appear in logs."""

    # Patterns that might contain secrets
    SENSITIVE_KEYS = {
        "api_key", "apikey", "api-key", "token", "secret",
        "password", "passwd", "credential", "authorization",
        "bearer", "access_token", "refresh_token",
    }

    @classmethod
    def sanitize(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Replace sensitive values with [REDACTED]."""
        sanitized = {}
        for key, value in data.items():
            if any(s in key.lower() for s in cls.SENSITIVE_KEYS):
                sanitized[key] = "[REDACTED]"
            elif isinstance(value, dict):
                sanitized[key] = cls.sanitize(value)
            elif isinstance(value, list):
                sanitized[key] = [
                    cls.sanitize(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                sanitized[key] = value
        return sanitized
