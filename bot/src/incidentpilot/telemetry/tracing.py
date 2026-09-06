"""OpenTelemetry tracing.

Spans wrap webhook -> stream -> worker -> relay -> external call. The trace
context is injected into the stream entry itself (W1-13), because the worker
consumes the entry in a different process minutes later and would otherwise
start an unparented trace -- losing exactly the causal link the trace exists to
show.

Tracing is off by default. An exporter that cannot reach a collector adds
latency to the ingest path, and the ingest path has a p99 SLO of 250 ms.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, Tracer

_TRACER_NAME = "incidentpilot"


def configure_tracing(
    *,
    enabled: bool = False,
    service_name: str = "incidentpilot-api",
    endpoint: str | None = None,
    exporter: SpanExporter | None = None,
) -> None:
    """Install a tracer provider. A no-op provider stays in place when disabled."""
    if not enabled:
        return

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    chosen = exporter
    if chosen is None and endpoint:
        # Imported lazily: the OTLP exporter pulls grpc/protobuf, and a
        # deployment that never enables tracing should not pay for that import.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )

        chosen = OTLPSpanExporter(endpoint=endpoint)

    if chosen is not None:
        provider.add_span_processor(BatchSpanProcessor(chosen))

    trace.set_tracer_provider(provider)


def get_tracer() -> Tracer:
    return trace.get_tracer(_TRACER_NAME)


def current_trace_id() -> str | None:
    """Hex trace id of the active span, for binding into log context."""
    span = trace.get_current_span()
    ctx: SpanContext = span.get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def carrier_for_stream() -> dict[str, str]:
    """W3C trace context, flattened for carriage in a stream entry.

    Keys are prefixed so they cannot collide with alert fields, and so the
    consumer can strip them without a hardcoded list of what tracing looks like
    this year.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return {f"otel.{k}": v for k, v in carrier.items()}


def context_from_stream(fields: dict[str, Any]) -> Any:
    """Rebuild the parent context from a stream entry's ``otel.*`` fields."""
    carrier = {
        key[len("otel.") :]: str(value) for key, value in fields.items() if key.startswith("otel.")
    }
    return extract(carrier) if carrier else None


__all__ = [
    "NonRecordingSpan",
    "carrier_for_stream",
    "configure_tracing",
    "context_from_stream",
    "current_trace_id",
    "get_tracer",
]
