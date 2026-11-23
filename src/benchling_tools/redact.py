"""Remove client secrets and access tokens from user-visible output.

Values shorter than 8 characters are not tracked. Benchling client secrets and
access tokens are longer than that, and shorter strings collide with ordinary
words such as an environment name.
"""

from __future__ import annotations

import threading
from typing import Any

_MIN_LENGTH = 8
_SENSITIVE_KEYS = frozenset(
    {"access_token", "authorization", "client_secret", "id_token", "refresh_token"}
)
_REDACTED = "[redacted]"
_lock = threading.Lock()
_secrets: set[str] = set()


def register_secret(value: object) -> None:
    """Remember a secret so later output can be scrubbed. Short values are ignored."""
    if not isinstance(value, str) or len(value) < _MIN_LENGTH:
        return
    with _lock:
        _secrets.add(value)


def registered_secrets() -> tuple[str, ...]:
    """Return the secrets tracked so far, for checks that must refuse rather than scrub."""
    with _lock:
        return tuple(_secrets)


def clear_registered_secrets() -> None:
    """Drop tracked secrets. Tests use this so cases do not affect each other."""
    with _lock:
        _secrets.clear()


def redact(value: Any) -> Any:
    """Return a copy with tracked secrets and credential fields removed."""
    with _lock:
        secrets = tuple(sorted(_secrets, key=len, reverse=True))
    return _redact(value, secrets)


def redact_text(value: str) -> str:
    """Scrub a single string."""
    redacted = redact(value)
    return redacted if isinstance(redacted, str) else _REDACTED


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, _REDACTED)
        return value
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = _redact(item, secrets)
        return cleaned
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, secrets) for item in value)
    return value
