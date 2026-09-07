"""Prometheus over httpx (W6-01).

Same reasoning as the paging adapter: plain HTTP so `respx` can record and
replay it, and so the week 7 corpus performs zero network calls (INV-10).

The only subtlety worth stating is the failure translation. Prometheus answers
`200 OK` with `{"status": "error"}` for a malformed query, so checking the HTTP
status alone would turn a broken PromQL expression into a successful query that
returned nothing -- and "no data" and "your query is wrong" mean very different
things to a document that is about to claim an impact number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from incidentpilot.adapters.metrics.base import (
    InstantValue,
    MetricsError,
    MetricsUnavailable,
    RangeSeries,
    series_key,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

# Short. This sits between `/resolve` and a posted PIR, and the answer to a slow
# Prometheus is the honest "unavailable" block, not a document that arrives
# ninety seconds late because one query hung.
TIMEOUT_S = 10.0


class PrometheusMetrics:
    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(self, expr: str, *, at: datetime) -> InstantValue:
        body = await self._get("/api/v1/query", {"query": expr, "time": _unix(at)})
        result = (body.get("data") or {}).get("result") or []
        if not result:
            # An empty result is not an error. "No 5xx in this window" is a real
            # and useful answer, and coercing it to unavailable would hide a
            # clean incident behind a degradation notice.
            return InstantValue(query=expr, value=None, start=at, end=at)

        first = result[0]
        raw = (first.get("value") or [None, None])[1]
        return InstantValue(
            query=expr,
            value=float(raw) if raw is not None else None,
            start=at,
            end=at,
            labels={str(k): str(v) for k, v in (first.get("metric") or {}).items()},
        )

    async def query_range(
        self, expr: str, *, start: datetime, end: datetime, step_s: int
    ) -> list[RangeSeries]:
        body = await self._get(
            "/api/v1/query_range",
            {"query": expr, "start": _unix(start), "end": _unix(end), "step": str(step_s)},
        )
        out: list[RangeSeries] = []
        for entry in (body.get("data") or {}).get("result") or []:
            labels = {str(k): str(v) for k, v in (entry.get("metric") or {}).items()}
            points = [
                (datetime.fromtimestamp(float(ts), UTC), float(value))
                for ts, value in (entry.get("values") or [])
            ]
            out.append(
                RangeSeries(query=expr, series=series_key(labels), points=points, labels=labels)
            )
        return out

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._base}{path}", params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise MetricsUnavailable(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 500:
            raise MetricsUnavailable(f"prometheus {response.status_code}")
        if response.status_code >= 400:
            raise MetricsError(f"prometheus {response.status_code}: {response.text[:200]}")

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise MetricsUnavailable("prometheus returned a non-JSON body") from exc

        # 200 OK with status=error is how Prometheus reports a bad expression.
        if body.get("status") != "success":
            raise MetricsError(f"prometheus error: {body.get('error', 'unknown')}")
        return body


def _unix(moment: datetime) -> str:
    return f"{moment.timestamp():.3f}"
