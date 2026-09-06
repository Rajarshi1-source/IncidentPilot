"""Liveness, readiness and metrics.

The distinction matters and is routinely got wrong:

``/healthz`` is **liveness** -- is this process wedged? It must not check
dependencies. A liveness probe that fails when Valkey is briefly unreachable
tells Kubernetes to kill a perfectly healthy pod, turning a dependency blip into
a restart storm at exactly the wrong moment.

``/readyz`` is **readiness** -- should this pod receive traffic? It checks the
dependencies it genuinely needs, so a pod that cannot reach Valkey is taken out
of the Service while staying alive and recovering.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from incidentpilot.api.deps import SettingsDep, ValkeyDep
from incidentpilot.telemetry.metrics import REGISTRY

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately checks nothing external."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response, settings: SettingsDep, valkey: ValkeyDep) -> dict[str, object]:
    """Readiness. Valkey must be reachable -- it is the ingest write path.

    Postgres is deliberately *not* required here. Under brownout (L2) the
    database is unreachable and alerts buffer to the write-ahead stream, and
    ingest is still doing its job: a dropped alert is unrecoverable, a delayed
    one is not. Failing readiness in that state would take the last working
    component out of the Service.
    """
    checks: dict[str, bool] = {}

    try:
        checks["valkey"] = bool(await valkey.ping())
    except Exception:
        checks["valkey"] = False

    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "environment": settings.environment,
    }


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint, served from our explicit registry."""
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
