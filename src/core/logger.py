"""Structured JSON logging with secret filtering and rotation."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


_SECRET_PATTERNS = [
    re.compile(r"(api_key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)", re.IGNORECASE),
    re.compile(r"(api_secret['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)", re.IGNORECASE),
    re.compile(r"(bot_token['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)", re.IGNORECASE),
    re.compile(r"(password['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)", re.IGNORECASE),
]

_MASK = "***REDACTED***"


def _mask_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(rf"\1{_MASK}", text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _mask_secrets(record.msg)
        return True


def _secret_processor(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    event = event_dict.get("event", "")
    if isinstance(event, str):
        event_dict["event"] = _mask_secrets(event)
    for key in ("api_key", "api_secret", "bot_token", "password"):
        if key in event_dict:
            event_dict[key] = _MASK
    return event_dict


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_dir: str = "logs",
) -> None:
    """Configure structlog + stdlib logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    # stdlib root handler
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    # console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.addFilter(SecretFilter())
    root.addHandler(console)

    # file handler with rotation
    file_handler = RotatingFileHandler(
        log_path / "cryptoforge.log",
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.addFilter(SecretFilter())
    root.addHandler(file_handler)

    # structlog processors
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _secret_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer(
            serializer=_orjson_dumps
        )
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    for handler in root.handlers:
        handler.setFormatter(formatter)


def _orjson_dumps(obj: object, **_kw: object) -> str:
    import orjson

    return orjson.dumps(obj).decode("utf-8")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
