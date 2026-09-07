"""PagerDuty over httpx, not the vendor SDK (W5-02).

Deliberately hand-rolled against the HTTP API. Three reasons, and the third is
the one that matters:

1. The SDK would pull a dependency tree for two endpoints.
2. `respx` can record and replay plain httpx traffic; an SDK's internal client
   is far harder to intercept, and the week 7 replay must perform **zero**
   network calls (INV-10).
3. **It keeps the adapter honest.** An SDK's own retry, backoff and error types
   would leak through this interface, and the next implementation -- the static
   rota, Opsgenie, anything -- would have to imitate them. Writing the HTTP by
   hand is what makes ``PagingAdapter`` a boundary rather than a thin wrapper
   around one vendor's client object.

Two API surfaces are used, and they are genuinely different products:

* **Events API v2** (``events.pagerduty.com``) creates the alert. Its
  ``dedup_key`` is the whole reason a retried page does not wake someone twice,
  and we pass the incident's ``public_key`` so the key is derived rather than
  generated.
* **REST API** (``api.pagerduty.com``) answers "who is on call", and needs a
  different token in a different header. Mixing the two up is the classic first
  mistake with this provider.
"""

from __future__ import annotations

from typing import Any

import httpx

from incidentpilot.adapters.paging.base import (
    OnCall,
    OnCallSource,
    PageResult,
    PermanentPagingError,
    RetryablePagingError,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
REST_BASE = "https://api.pagerduty.com"

# Short on purpose. This call sits on the outbox relay's path, and the relay's
# answer to a slow provider is to defer the row and come back -- which is
# strictly better than holding a worker open waiting for a pager API.
TIMEOUT_S = 5.0

SEVERITY_MAP = {"sev1": "critical", "sev2": "error", "sev3": "warning", "sev4": "info"}


class PagerDutyPaging:
    """Events API v2 for paging, REST API for the schedule."""

    def __init__(
        self,
        *,
        routing_key: str,
        api_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._routing_key = routing_key
        self._api_token = api_token
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- read ------------------------------------------------------------

    async def oncall_for(self, schedule: str) -> OnCall:
        if not self._api_token:
            raise PermanentPagingError("no PagerDuty REST token configured")

        response = await self._request(
            "GET",
            f"{REST_BASE}/oncalls",
            headers={
                "Authorization": f"Token token={self._api_token}",
                "Accept": "application/vnd.pagerduty+json;version=2",
            },
            params={"schedule_ids[]": schedule, "limit": 10},
        )
        entries = response.get("oncalls") or []

        # Ordered by escalation level: level 1 is the primary. Taking entries[0]
        # blindly is wrong -- the API does not promise that ordering, and on a
        # multi-level policy it will eventually hand back level 2 first.
        by_level = sorted(entries, key=lambda e: int(e.get("escalation_level") or 99))
        users = [str((e.get("user") or {}).get("id") or "") for e in by_level]
        users = [u for u in users if u]

        return OnCall(
            schedule=schedule,
            primary=users[0] if users else None,
            secondary=users[1] if len(users) > 1 else None,
            source=OnCallSource.PROVIDER,
        )

    # -- write (relay only, INV-03) --------------------------------------

    async def page(
        self,
        user: str,
        *,
        incident_key: str,
        title: str,
        severity: str,
        url: str | None = None,
    ) -> PageResult:
        payload: dict[str, Any] = {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            # Derived, never generated. A retry after a crash between the page
            # and its record must land on the same alert.
            "dedup_key": incident_key,
            "payload": {
                "summary": title[:1024],
                "source": "incidentpilot",
                "severity": SEVERITY_MAP.get(severity, "error"),
                "custom_details": {"responder": user, "incident": incident_key},
            },
        }
        if url:
            payload["links"] = [{"href": url, "text": "War room"}]

        body = await self._request("POST", EVENTS_URL, json=payload)
        return PageResult(
            responder=user,
            delivered=str(body.get("status")) == "success",
            ref=str(body.get("dedup_key") or incident_key),
        )

    # -- transport -------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """One place where an HTTP status becomes a typed error.

        The split matters to the relay: a retryable error defers the outbox row
        and comes back with backoff, a permanent one marks it dead immediately
        rather than spending eight retries on a call that cannot succeed.
        """
        try:
            response = await self._client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryablePagingError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryablePagingError(f"pagerduty {response.status_code}")
        if response.status_code >= 400:
            raise PermanentPagingError(f"pagerduty {response.status_code}: {response.text[:200]}")

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RetryablePagingError("pagerduty returned a non-JSON body") from exc
        return body
