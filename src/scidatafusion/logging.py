"""Structured logging with defensive redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from pydantic import SecretStr

_SENSITIVE_KEY: Final[re.Pattern[str]] = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|secret|token)", re.IGNORECASE
)
_QUERY_SECRET: Final[re.Pattern[str]] = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|key|password|secret|token)=)[^&\s]+"
)
_AUTHORIZATION_SECRET: Final[re.Pattern[str]] = re.compile(
    r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_SECRET: Final[re.Pattern[str]] = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+")
_INLINE_SECRET: Final[re.Pattern[str]] = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|cookie|credential|password|"
    r"secret|token)[\"']?\s*[=:]\s*[\"']?)[^\"'\s,;&}]+"
)
_PROVIDER_SECRET: Final[re.Pattern[str]] = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|" + "LTAI" + r"[A-Za-z0-9]{12,})\b"
)
_STANDARD_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
)


def _redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(r"\1[REDACTED]", value)
    value = _AUTHORIZATION_SECRET.sub(r"\1[REDACTED]", value)
    value = _BEARER_SECRET.sub(r"\1[REDACTED]", value)
    value = _INLINE_SECRET.sub(r"\1[REDACTED]", value)
    return _PROVIDER_SECRET.sub("[REDACTED]", value)


def _sanitize(value: object, *, key: str | None = None) -> object:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per log line for local and hosted observability."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_"):
                payload[key] = _sanitize(value, key=key)
        if record.exc_info:
            payload["exception"] = _redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger exactly once per process invocation."""

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
