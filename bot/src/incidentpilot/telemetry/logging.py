"""Structured logging.

Every line is JSON with ``incident_id``, ``trace_id`` and ``state`` bound into
context, so a single grep (or Loki query) reconstructs one incident end to end.
That property is the reason for contextvars rather than passing a logger around:
the binding survives across ``await`` points and into the handlers the relay
calls, without every function signature growing a logger parameter.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and route stdlib logging through it.

    Third-party libraries (uvicorn, httpx, sqlalchemy) log via stdlib. Without
    the ProcessorFormatter below they would emit unstructured lines into an
    otherwise JSON stream, which breaks every downstream parser at exactly the
    moment you need the logs.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; clear them or every request is logged
    # twice, once structured and once not.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers.clear()
        stdlib_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


@contextmanager
def incident_context(
    *,
    incident_id: int | str | None = None,
    trace_id: str | None = None,
    state: str | None = None,
    **extra: Any,
) -> Iterator[None]:
    """Bind incident identity for the duration of a block.

    Unbinds only what it bound, so nesting a narrower context inside a broader
    one does not tear down the outer bindings on exit.
    """
    bindings: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    if incident_id is not None:
        bindings["incident_id"] = incident_id
    if trace_id is not None:
        bindings["trace_id"] = trace_id
    if state is not None:
        bindings["state"] = state

    bind_contextvars(**bindings)
    try:
        yield
    finally:
        unbind_contextvars(*bindings.keys())


def reset_context() -> None:
    """Drop all bound context. Called between requests."""
    clear_contextvars()
