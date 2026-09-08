"""Seed the demo dataset (W8-14, W8-09).

    uv run --directory bot python ../scripts/seed.py --reset

What this is for: the public demo has to show *history* -- MTTR trends, storm
compression over weeks, action items aging, cost per PIR by prompt version. A
live run produces one incident. Charts of one point are not charts.

**These rows are synthetic and the dashboard says so.** Every seeded incident
carries ``[demo]`` in its title and the UI renders a banner in ``DEMO_MODE``.
A portfolio dashboard that presented invented numbers as production data would
be the same category of dishonesty this project spends six weeks preventing in
the PIR.

Deterministic: a fixed seed, fixed base instant, no ``uuid``. Two runs produce
the same database, which is what makes a screenshot reproducible and a demo
rehearsable.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot" / "src"))

from sqlalchemy import text  # noqa: E402

from incidentpilot.config.settings import Settings  # noqa: E402
from incidentpilot.db.engine import build_engine, build_session_factory  # noqa: E402
from incidentpilot.runtime import configure_event_loop  # noqa: E402

# Fixed, so two runs produce the same database. A demo whose numbers move
# between rehearsal and interview is a demo you stop trusting mid-sentence.
SEED = 20260908
BASE = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)

SERVICES: list[tuple[str, str, int]] = [
    ("postgres-primary", "data", 0),
    ("postgres-replica", "data", 1),
    ("payments-api", "payments", 2),
    ("payments-worker", "payments", 2),
    ("checkout-web", "storefront", 3),
    ("catalog-api", "catalog", 2),
    ("valkey-cache", "platform", 0),
    ("kafka-events", "platform", 0),
    ("notifications", "platform", 1),
    ("search-index", "catalog", 1),
    ("session-store", "storefront", 1),
    ("upi-gateway", "payments", 0),
]

# (title, severity, service, alerts absorbed, tta_s, ttm_s, mttr_s)
# Shaped like a quarter of real operations rather than uniformly: a few long
# Sev1s, a tail of quick Sev3s. A seeded dataset with a flat distribution makes
# every percentile chart look identical and teaches a viewer nothing.
INCIDENTS: list[tuple[str, str, str, int, int, int, int]] = [
    ("[demo] postgres primary lost its lease", "sev1", "postgres-primary", 40, 55, 1_450, 2_100),
    ("[demo] checkout errors after a bad deploy", "sev1", "payments-api", 12, 40, 900, 1_500),
    ("[demo] replica lag during a backfill", "sev2", "postgres-replica", 8, 95, 1_800, 2_600),
    ("[demo] consumer group stalled on a poison message", "sev2", "kafka-events", 5, 120, 2_400, 3_100),
    ("[demo] cache evictions after a config push", "sev2", "valkey-cache", 4, 80, 700, 1_200),
    ("[demo] storefront latency from a slow dependency", "sev2", "checkout-web", 6, 70, 1_100, 1_900),
    ("[demo] worker OOM after a memory limit change", "sev1", "payments-worker", 9, 35, 640, 1_050),
    ("[demo] indexer wedged behind a schema change", "sev3", "search-index", 3, 300, 3_600, 5_400),
    ("[demo] session writes failing on a full disk", "sev2", "session-store", 4, 110, 950, 1_600),
    ("[demo] upstream gateway returning errors", "sev2", "upi-gateway", 7, 60, 2_900, 3_400),
    ("[demo] catalog errors from replica lag", "sev3", "catalog-api", 5, 240, 1_500, 2_200),
    ("[demo] notification pods stuck unready", "sev3", "notifications", 3, 280, 800, 1_400),
    ("[demo] payments settlement backlog", "sev1", "payments-worker", 11, 45, 1_900, 2_800),
    ("[demo] TLS certificate expiry warning", "sev3", "checkout-web", 1, 600, 400, 900),
    ("[demo] kafka consumer lag on notifications", "sev3", "notifications", 2, 320, 1_200, 1_700),
    ("[demo] postgres connections exhausted", "sev1", "postgres-primary", 14, 50, 1_100, 1_800),
    ("[demo] search index stale after reindex", "sev3", "search-index", 2, 400, 2_000, 2_900),
    ("[demo] valkey hit rate collapse", "sev2", "valkey-cache", 3, 90, 850, 1_300),
]

ACTIONS: list[tuple[str, str, str]] = [
    ("P0", "monitoring", "add a replica lag alert with a page threshold"),
    ("P1", "documentation", "write the postgres promotion runbook"),
    ("P1", "reliability", "add a soak window before a resolve is accepted"),
    ("P2", "process", "split the war room by workstream on Sev1s"),
    ("P1", "monitoring", "alert when prometheus is unreachable"),
    ("P2", "observability", "add tracing coverage to the checkout path"),
    ("P0", "reliability", "cap the connection pool per worker replica"),
    ("P2", "documentation", "translate the runbook into the language the team uses"),
]


async def seed(reset: bool) -> None:
    rng = random.Random(SEED)
    engine = build_engine(Settings(environment="local"))
    sessions = build_session_factory(engine)

    async with sessions() as session, session.begin():
        if reset:
            # Only the demo rows. A seeder that truncated everything would be a
            # foot-gun pointed at a real deployment the first time someone ran
            # it against the wrong DATABASE_URL.
            await session.execute(
                text("DELETE FROM incidents WHERE title LIKE '[demo]%'")
            )

        for name, team, depth in SERVICES:
            await session.execute(
                text(
                    """
                    INSERT INTO services (name, team, tier, graph_depth)
                    VALUES (:name, :team, 1, :depth)
                    ON CONFLICT (name) DO UPDATE SET graph_depth = EXCLUDED.graph_depth
                    """
                ),
                {"name": name, "team": team, "depth": depth},
            )

        service_ids = {
            str(r["name"]): int(r["id"])
            for r in (await session.execute(text("SELECT id, name FROM services"))).mappings()
        }

        for index, (title, severity, service, alerts, tta, ttm, mttr) in enumerate(INCIDENTS):
            # Spread across ~10 weeks so the weekly compression chart and the
            # 30-day MTTR window both have something to draw.
            detected = BASE - timedelta(days=rng.randint(1, 70), hours=rng.randint(0, 20))
            key = f"INC-DEMO-{index + 1:03d}"
            incident_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO incidents
                            (public_key, dedup_key, dedup_epoch, title, severity, state,
                             primary_service_id, correlated_alert_count,
                             detected_at, acknowledged_at, mitigated_at, resolved_at, closed_at,
                             chat_channel_id, chat_channel_name)
                        VALUES
                            (:key, :dedup, 0, :title, CAST(:sev AS severity_level), 'closed',
                             :svc, :alerts,
                             :detected, :ack, :mitigated, :resolved, :resolved,
                             :channel, :channel_name)
                        ON CONFLICT (dedup_key, dedup_epoch) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "key": key,
                        "dedup": f"demo:{index}",
                        "title": title,
                        "sev": severity,
                        "svc": service_ids.get(service),
                        "alerts": alerts,
                        "detected": detected,
                        "ack": detected + timedelta(seconds=tta),
                        "mitigated": detected + timedelta(seconds=ttm),
                        "resolved": detected + timedelta(seconds=mttr),
                        "channel": f"C{index + 900:06d}",
                        "channel_name": f"inc-demo-{index + 1:03d}",
                    },
                )
            ).scalar()

            if incident_id is None:
                continue

            # A PIR per incident, most fully grounded. Two skeletons, because
            # the honest picture includes the days the provider was unavailable
            # -- and because a flat 100% line with no explanation invites the
            # question "what happens when it fails?" with no answer on screen.
            layer = "skeleton" if index in (7, 14) else "llm_primary"
            coverage = 0.0 if layer == "skeleton" else 1.0
            await session.execute(
                text(
                    """
                    INSERT INTO pir_documents
                        (incident_id, revision, generation_layer, body, rendered_markdown,
                         provider, model, prompt_version, prompt_sha256,
                         input_tokens, output_tokens, cost_usd, generation_ms,
                         citation_coverage, validation_passed, human_edit_ratio, created_at)
                    VALUES
                        (:incident_id, 1, :layer, '{}'::jsonb, :markdown,
                         :provider, :model, :pv, :sha,
                         :in_tok, :out_tok, :cost, :ms,
                         :coverage, :passed, :edit, :created)
                    """
                ),
                {
                    "incident_id": incident_id,
                    "layer": layer,
                    "markdown": f"# {title}\n\n(demo dataset)",
                    "provider": None if layer == "skeleton" else "openai",
                    "model": None if layer == "skeleton" else "gpt-5.4-mini",
                    "pv": "2.1.0",
                    "sha": "demo",
                    "in_tok": 0 if layer == "skeleton" else rng.randint(7_000, 12_000),
                    "out_tok": 0 if layer == "skeleton" else rng.randint(800, 1_400),
                    "cost": 0.0 if layer == "skeleton" else round(rng.uniform(0.004, 0.02), 4),
                    "ms": rng.randint(9_000, 22_000),
                    "coverage": coverage,
                    "passed": layer != "skeleton",
                    "edit": round(rng.uniform(0.02, 0.28), 3),
                    "created": detected + timedelta(seconds=mttr + 120),
                },
            )

            for priority, category, description in rng.sample(ACTIONS, k=rng.randint(1, 3)):
                done = rng.random() < 0.55
                await session.execute(
                    text(
                        """
                        INSERT INTO action_items
                            (incident_id, description, citations, priority, category,
                             status, created_at, completed_at)
                        VALUES
                            (:incident_id, :description, '[]'::jsonb, :priority, :category,
                             :status, :created, :completed)
                        """
                    ),
                    {
                        "incident_id": incident_id,
                        "description": description,
                        "priority": priority,
                        "category": category,
                        "status": "done" if done else "open",
                        "created": detected + timedelta(seconds=mttr + 300),
                        "completed": (
                            detected + timedelta(days=rng.randint(2, 40)) if done else None
                        ),
                    },
                )

        # One deployment ledger row so ops/models has something to render, and
        # so the page shows what a promotion with evidence looks like.
        await session.execute(
            text(
                """
                INSERT INTO model_deployments
                    (role, provider, model, state, traffic_pct, prompt_version,
                     eval_run_id, promoted_by, started_at)
                VALUES ('synthesize', 'openai', 'gpt-5.4-mini', 'default', 100, '2.1.0',
                        'demo-eval-run', 'seed.py', :started)
                ON CONFLICT DO NOTHING
                """
            ),
            {"started": BASE - timedelta(days=70)},
        )

    await engine.dispose()


async def summarise() -> dict[str, Any]:
    engine = build_engine(Settings(environment="local"))
    async with engine.connect() as conn:
        rows = {}
        for label, sql in (
            ("incidents", "SELECT count(*) FROM incidents WHERE title LIKE '[demo]%'"),
            ("pirs", "SELECT count(*) FROM pir_documents"),
            ("action_items", "SELECT count(*) FROM action_items"),
            ("services", "SELECT count(*) FROM services"),
        ):
            rows[label] = (await conn.execute(text(sql))).scalar()
    await engine.dispose()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing [demo] rows first (never touches real incidents)",
    )
    args = parser.parse_args()

    configure_event_loop()
    asyncio.run(seed(args.reset))
    counts = asyncio.run(summarise())
    print("seeded the demo dataset:")
    for key, value in counts.items():
        print(f"  {key:14} {value}")
    print("\nevery seeded incident is titled '[demo] ...' and the UI banners it in DEMO_MODE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
