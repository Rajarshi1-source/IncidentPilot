"""Loads the service graph from YAML (or TraceMap) into a pure ``ServiceGraph``.

This lives in ``config/`` and not in ``domain/`` on purpose: reading a file is
I/O, and ``domain/`` may not do I/O (INV-01, enforced by
``test_domain_never_calls_an_io_builtin``). The graph *structure* is pure; the
*acquisition* of it is not, and the seam between them is what lets the eval
harness build a graph from a fixture with no filesystem at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from incidentpilot.domain.graph import ServiceGraph
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

DEFAULT_PATH = Path(__file__).parent / "service-graph.yaml"


def load_service_graph(path: Path | str | None = None) -> ServiceGraph:
    """Read the checked-in graph.

    A missing or malformed file degrades to an empty graph rather than raising.
    Correlation without topology is worse -- it falls back to time and label
    overlap only -- but it still works, and refusing to start the ingest path
    because a YAML file is absent would be the wrong trade for a component whose
    entire job is to be available during someone else's outage.
    """
    target = Path(path) if path else DEFAULT_PATH

    if not target.exists():
        log.warning("service_graph.missing", path=str(target))
        return ServiceGraph.empty()

    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        log.error("service_graph.malformed", path=str(target), error=str(exc))
        return ServiceGraph.empty()

    if not isinstance(raw, dict):
        log.error("service_graph.not_a_mapping", path=str(target))
        return ServiceGraph.empty()

    services = raw.get("services")
    if not isinstance(services, dict):
        log.error("service_graph.no_services_key", path=str(target))
        return ServiceGraph.empty()

    graph = ServiceGraph.from_mapping(services)
    log.info("service_graph.loaded", path=str(target), services=len(graph.services()))
    return graph
