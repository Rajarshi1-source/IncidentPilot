"""INV-11: the "IncidentPilot is down" alert must not route through IncidentPilot.

This is a configuration invariant, so it gets a configuration test. Without one
it is a comment in a YAML file that survives exactly until someone reorganizes
the routing tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CONFIG = Path(__file__).resolve().parents[3] / "monitoring" / "alertmanager" / "alertmanager.yml"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    assert CONFIG.exists(), f"alertmanager config missing at {CONFIG}"
    loaded: dict[str, Any] = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return loaded


def _receiver_names(config: dict[str, Any]) -> set[str]:
    return {r["name"] for r in config.get("receivers", [])}


def test_fallback_receiver_exists(config: dict[str, Any]) -> None:
    assert "sre-fallback" in _receiver_names(config)


def test_fallback_route_present(config: dict[str, Any]) -> None:
    """A route matching `route="fallback"` must target the fallback receiver."""
    routes = config["route"].get("routes", [])
    matching = [
        r
        for r in routes
        if any(
            'route = "fallback"' in m or "route=fallback" in m.replace(" ", "")
            for m in r.get("matchers", [])
        )
    ]
    assert matching, "no route matches the `route: fallback` label"
    assert all(r["receiver"] == "sre-fallback" for r in matching)


def test_incidentpilot_down_bypasses_the_bot(config: dict[str, Any]) -> None:
    """The meta-alert must reach a receiver that does not post to the bot."""
    routes = config["route"].get("routes", [])
    matching = [r for r in routes if any("IncidentPilotDown" in m for m in r.get("matchers", []))]
    assert matching, "IncidentPilotDown has no dedicated route"

    for route in matching:
        assert route["receiver"] == "sre-fallback"
        # continue: false stops it falling through to the default receiver,
        # which is the bot. Without this the bypass is decorative.
        assert route.get("continue") is False, (
            "IncidentPilotDown must not continue to the default receiver"
        )


def test_fallback_receiver_has_no_webhook_to_the_bot(config: dict[str, Any]) -> None:
    """The circular dependency, asserted directly."""
    fallback = next(r for r in config["receivers"] if r["name"] == "sre-fallback")
    for webhook in fallback.get("webhook_configs", []):
        url = webhook.get("url", "")
        assert "/webhooks/alertmanager" not in url, (
            "the fallback receiver posts back into IncidentPilot -- "
            "that is the circular dependency INV-11 exists to prevent"
        )


def test_fallback_routes_are_ordered_before_the_default(config: dict[str, Any]) -> None:
    """Alertmanager takes the first matching route; ordering is the mechanism."""
    routes = config["route"].get("routes", [])
    fallback_indexes = [i for i, r in enumerate(routes) if r.get("receiver") == "sre-fallback"]
    other_indexes = [i for i, r in enumerate(routes) if r.get("receiver") != "sre-fallback"]
    assert fallback_indexes, "no fallback routes at all"
    if other_indexes:
        assert max(fallback_indexes) < min(other_indexes), (
            "a non-fallback route precedes the fallback routes; "
            "IncidentPilotDown could be captured by it first"
        )
