"""Configuration invariants, metric name stability, and log structure."""

from __future__ import annotations

import json
import logging

import pytest
from prometheus_client import generate_latest

from incidentpilot.config.assert_invariants import (
    ConfigInvariantError,
    assert_invariants,
    check,
)
from incidentpilot.config.settings import Settings
from incidentpilot.telemetry.logging import configure_logging, get_logger, incident_context
from incidentpilot.telemetry.metrics import REGISTRY

# The full public metric surface. Grafana dashboards and the burn-rate alert
# rules match on these names, so a rename is a breaking change -- this snapshot
# makes that explicit rather than discovering it via an empty panel.
EXPECTED_METRICS = {
    "ip_webhook_seconds",
    "ip_time_to_war_room_seconds",
    "ip_time_to_acknowledge_seconds",
    "ip_transitions_total",
    "ip_alerts_per_incident",
    "ip_stream_pending",
    "ip_pir_seconds",
    "ip_pir_citation_coverage",
    "ip_pir_human_edit_ratio",
    "ip_llm_cost_usd_total",
    "ip_budget_trips_total",
    "ip_transcript_completeness",
    "ip_degradation_level",
    "ip_slack_rate_limited_total",
    "ip_duplicate_messages_total",
    "ip_webhook_rejected_total",
    "ip_alerts_accepted_total",
    "ip_brownout_buffered_total",
    "ip_outbox_dead_total",
    "ip_outbox_dispatched_total",
    "ip_messages_stored_total",
    "ip_slack_history_calls_total",
    "ip_llm_calls_total",
    "ip_pir_validation_failures_total",
    "ip_pii_redacted_total",
}


def test_metric_names_stable() -> None:
    """Assert on the *exposition* names, not ``collect().name``.

    prometheus_client strips the ``_total`` suffix from a Counter's internal
    name, so ``collect()`` reports ``ip_alerts_accepted`` while the scrape
    exposes ``ip_alerts_accepted_total``. Grafana and the alert rules query the
    exposed name, so that is the contract worth freezing -- checking the
    internal name would let a real dashboard break while the test stayed green.
    """
    text = generate_latest(REGISTRY).decode()
    exposed = {line.split()[2] for line in text.splitlines() if line.startswith("# HELP")}

    missing = EXPECTED_METRICS - exposed
    assert not missing, f"metrics disappeared (breaking change for dashboards): {sorted(missing)}"


def test_settings_load_without_secrets() -> None:
    """C-01: a clean clone must start with no credentials at all."""
    cfg = Settings(environment="dev")
    assert cfg.slack_signing_secret is None
    assert cfg.alertmanager_bearer is None
    assert cfg.paging_webhook_secret is None
    assert cfg.is_dev
    assert check(cfg) == []


def test_prod_config_rejects_missing_secret() -> None:
    """The requirement is real -- it just moves from import time to startup."""
    cfg = Settings(environment="prod", chat_provider="slack", metrics_provider="prometheus")
    problems = check(cfg)
    assert any("alertmanager_bearer" in p for p in problems)
    assert any("slack_signing_secret" in p for p in problems)
    assert any("paging_webhook_secret" in p for p in problems)

    with pytest.raises(ConfigInvariantError, match="alertmanager_bearer"):
        assert_invariants(cfg)


def test_citations_cannot_be_disabled_in_any_environment() -> None:
    """INV-05 is not a feature flag, not even in dev."""
    cfg = Settings(environment="dev", require_citations=False)
    problems = check(cfg)
    assert any("require_citations" in p for p in problems)


def test_prod_rejects_fake_providers() -> None:
    """A production deployment on fake adapters looks healthy and does nothing."""
    cfg = Settings(
        environment="prod",
        alertmanager_bearer="a",
        slack_signing_secret="b",
        paging_webhook_secret="c",
        chat_provider="fake",
        metrics_provider="fake",
    )
    problems = check(cfg)
    assert any("chat_provider" in p for p in problems)
    assert any("metrics_provider" in p for p in problems)


def test_prod_rejects_demo_mode() -> None:
    cfg = Settings(
        environment="prod",
        alertmanager_bearer="a",
        slack_signing_secret="b",
        paging_webhook_secret="c",
        chat_provider="slack",
        metrics_provider="prometheus",
        demo_mode=True,
    )
    assert any("demo_mode" in p for p in check(cfg))


def test_valid_prod_config_passes() -> None:
    cfg = Settings(
        environment="prod",
        alertmanager_bearer="a",
        slack_signing_secret="b",
        paging_webhook_secret="c",
        chat_provider="slack",
        metrics_provider="prometheus",
    )
    assert check(cfg) == []


def test_log_line_is_json_with_incident_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=True)
    log = get_logger("test")
    with incident_context(incident_id=204, trace_id="abc123", state="triaging"):
        log.info("transition.applied", to_state="engaged")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "transition.applied"
    assert payload["incident_id"] == 204
    assert payload["trace_id"] == "abc123"
    assert payload["state"] == "triaging"
    assert payload["to_state"] == "engaged"


def test_incident_context_unbinds_on_exit(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=True)
    log = get_logger("test")
    with incident_context(incident_id=7):
        log.info("inside")
    log.info("outside")

    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[-2])["incident_id"] == 7
    assert "incident_id" not in json.loads(lines[-1])


def test_stdlib_logs_are_routed_through_structlog(capsys: pytest.CaptureFixture[str]) -> None:
    """Third-party libraries log via stdlib; unstructured lines in a JSON
    stream break every downstream parser at the moment you need the logs."""
    configure_logging(level="INFO", json_output=True)
    logging.getLogger("uvicorn.error").warning("third party message")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["event"] == "third party message"


def test_socket_mode_is_rejected_outside_development() -> None:
    """§16: Socket Mode has no signature to verify and no boundary in front of it.

    Checked here as well as in the runner because a deployment must fail at
    startup, not on the first event -- by which point it has been accepting
    unauthenticated instructions from a WebSocket for however long it took
    someone to notice.
    """
    cfg = Settings(
        environment="prod",
        chat_provider="slack",
        metrics_provider="prometheus",
        slack_socket_mode=True,
    )
    assert any("slack_socket_mode" in p for p in check(cfg))
    assert not any("slack_socket_mode" in p for p in check(Settings(environment="dev")))


def test_the_history_budget_cannot_be_shortened_in_production() -> None:
    """Shortening it does not buy calls, it buys 429s -- and a 429 costs the
    next window too (INV-02)."""
    cfg = Settings(
        environment="prod",
        chat_provider="slack",
        metrics_provider="prometheus",
        history_budget_period_s=5,
    )
    assert any("history_budget_period_s" in p for p in check(cfg))


def test_transcript_ratio_starts_unmeasured_not_zero() -> None:
    """A fresh pod must not claim it lost the entire transcript.

    prometheus_client Gauges read 0 from definition, and 0 on this SLI is
    "every message is gone" -- which, with the tightest target in the table
    behind it, would page someone on every deploy.
    """
    import math

    from incidentpilot.telemetry.metrics import (
        TRANSCRIPT_RATIO,
        mark_transcript_ratio_unmeasured,
    )

    mark_transcript_ratio_unmeasured()
    # No public reader on a Gauge; this is the value the scrape would expose.
    value = TRANSCRIPT_RATIO._value.get()
    assert math.isnan(value)
    assert not (value < 0.9999), "a NaN must not satisfy an alert-rule comparison"


def test_citation_coverage_starts_unmeasured() -> None:
    """A fresh pod must not look like a grounding violation.

    `CitationCoverageBelowOne` is the one alert rule with no burn rate and no
    averaging window -- grounding is an invariant, not a target, so one minute
    below 1.0 pages. A Gauge reads 0 from definition, and 0 here is
    indistinguishable from "a published PIR carries an uncited claim".

    Week 8's demo proved it end to end: the stack came up, Prometheus scraped
    the zero, Alertmanager fired, and IncidentPilot opened an incident about
    itself through its own webhook. The self-monitoring was working; the metric
    was lying.
    """
    import math

    from incidentpilot.telemetry.metrics import (
        PIR_CITATION_COV,
        mark_citation_coverage_unmeasured,
    )

    mark_citation_coverage_unmeasured()
    value = PIR_CITATION_COV._value.get()
    assert math.isnan(value)
    assert not (value < 1), "a NaN must not satisfy the CitationCoverageBelowOne comparison"
