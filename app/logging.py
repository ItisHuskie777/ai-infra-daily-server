import contextvars
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from logging import Formatter as _LoggingFormatter
from logging import Logger as _LoggingLogger
from logging import LogRecord as _LoggingRecord
from logging import StreamHandler as _LoggingStreamHandler
from logging import getLogger as _get_stdlib_logger  # noqa: N813
from typing import override

_STANDARD_ATTRIBUTES = frozenset(vars(_LoggingRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
}
_context: contextvars.ContextVar[dict[str, object]] = contextvars.ContextVar(
    "logging_context"
)
_is_configured = False


class _JsonFormatter(_LoggingFormatter):
    @override
    def format(self, record: _LoggingRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_context.get({}),
        }

        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRIBUTES:
                payload[key] = value

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(*, level: str) -> None:
    """Installs the JSON handler on the root logger; idempotent."""
    global _is_configured  # noqa: PLW0603

    if _is_configured:
        return

    handler = _LoggingStreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = _get_stdlib_logger()
    root.addHandler(handler)
    root.setLevel(level)
    _is_configured = True


def get_logger(name: str) -> _LoggingLogger:
    return _get_stdlib_logger(name)


@contextmanager
def scoped(**fields: object) -> Iterator[None]:
    """Attaches fields (a run id, a source id) to every log line inside the block."""
    token = _context.set({**_context.get({}), **fields})

    try:
        yield
    finally:
        _context.reset(token)
