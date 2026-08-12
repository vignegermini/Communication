"""Structured logging. Emits either one JSON object per line (machine-parseable,
the default) or a readable text line. Extra fields passed via `logger.info(msg,
extra={"extra_fields": {...}})` are merged into the JSON output.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def setup_logging(level: str = "INFO", as_json: bool = True,
                  file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fmt: logging.Formatter
    if as_json:
        fmt = _JsonFormatter()
    else:
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
        )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(file)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # websockets is chatty at DEBUG; keep it at WARNING unless we're debugging.
    if level.upper() != "DEBUG":
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("aiohttp").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Helper to attach structured fields to a log record."""
    logger.log(level, msg, extra={"extra_fields": fields})
