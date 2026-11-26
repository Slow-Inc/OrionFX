"""Structured JSON logs with secret redaction (PRD-08 FR-010)."""

import json
import logging
from collections.abc import Iterable
from typing import Any

from app.core.secrets import redact


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Pass structured data as `extra={"fields": {...}}`."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "fields": getattr(record, "fields", {}),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(data, self._secrets), default=str)
