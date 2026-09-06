"""Startup and CI guard for configuration that must not be wrong in production.

This exists because of conflict C-01. Rev 2 makes the webhook secrets required
at import time, which would break the clean-clone demo. Making them optional
without this module would let a production deployment start with an unguarded
webhook -- so the requirement moves here, where it can tell dev from prod.

Run as a module in CI:

    python -m incidentpilot.config.assert_invariants --environment prod
"""

from __future__ import annotations

import argparse
import sys

from incidentpilot.config.settings import Settings, settings


class ConfigInvariantError(RuntimeError):
    """Configuration violates an invariant that must hold in this environment."""


def check(cfg: Settings) -> list[str]:
    """Return a list of violations. Empty means the configuration is sound."""
    problems: list[str] = []

    # INV-05 / B-09: grounding is not a feature flag. A PIR that can publish an
    # uncited claim is the failure this whole project exists to prevent, so the
    # switch that would allow it must never be off outside a test.
    if not cfg.require_citations:
        problems.append(
            "require_citations is False -- the grounding invariant (INV-05) cannot be disabled"
        )

    if cfg.is_dev:
        # Dev deliberately runs credential-free so the demo works on a clean
        # clone. Everything below is a production concern.
        return problems

    # B-02: three sources, three trust models. Each endpoint needs its own
    # credential, and a missing one means that endpoint is open to anyone who
    # can reach the pod.
    required_secrets = {
        "alertmanager_bearer": "POST /webhooks/alertmanager would accept unauthenticated alerts",
        "slack_signing_secret": "POST /webhooks/slack could not verify signatures",
        "paging_webhook_secret": "POST /webhooks/paging could not verify signatures",
    }
    for name, consequence in required_secrets.items():
        if getattr(cfg, name) is None:
            problems.append(f"{name} is unset in environment {cfg.environment!r}: {consequence}")

    if cfg.demo_mode:
        problems.append(
            f"demo_mode is True in environment {cfg.environment!r} -- demo mode serves "
            "synthetic fixtures and disables outbound writes; it is never a production posture"
        )

    # A production deployment pointing at the fake adapters would look healthy
    # and do nothing. That failure is silent, which makes it worth a check.
    if cfg.chat_provider == "fake":
        problems.append(f"chat_provider is 'fake' in environment {cfg.environment!r}")
    if cfg.metrics_provider == "fake":
        problems.append(
            f"metrics_provider is 'fake' in environment {cfg.environment!r} -- "
            "impact would be fabricated rather than computed (B-10)"
        )

    if cfg.signature_max_age_s > 300:
        problems.append(
            f"signature_max_age_s is {cfg.signature_max_age_s}s; the Slack replay window "
            "must not exceed 300s"
        )

    return problems


def assert_invariants(cfg: Settings | None = None) -> None:
    """Raise if configuration violates an invariant. Called from the lifespan."""
    problems = check(cfg or settings)
    if problems:
        raise ConfigInvariantError(
            "configuration invariants violated:\n  - " + "\n  - ".join(problems)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify configuration invariants.")
    parser.add_argument(
        "--environment",
        help="Override the environment to check (e.g. 'prod'), for CI use.",
    )
    args = parser.parse_args(argv)

    cfg = settings
    if args.environment:
        cfg = settings.model_copy(update={"environment": args.environment})

    problems = check(cfg)
    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        return 1
    print(f"config invariants OK (environment={cfg.environment})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
