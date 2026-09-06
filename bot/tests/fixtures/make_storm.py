"""Generate the storm fixtures (W2-14).

Run: ``uv run python tests/fixtures/make_storm.py``

The 40-alert fixture is the G2 gate and the demo's opening moment, so it models
a real cascade rather than forty copies of one alert: a Postgres primary fails,
the replica lags, then everything that depends on either starts shouting. If the
fixture were forty identical alerts, correlation would pass on label overlap
alone and the dependency-graph signal -- the actual differentiator -- would
never be exercised.

Committed as JSON so the corpus is stable and diffable; this script exists to
regenerate it, not to be imported at test time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
T0 = datetime(2026, 9, 6, 9, 14, 2, tzinfo=UTC)
CLUSTER = "prod-ap-south-1"

# (offset_seconds, alertname, service, namespace, severity, summary)
# Ordered by when each alert actually fires. The root cause is first and
# deepest; the symptoms arrive over the following four minutes, which is what a
# real cascade looks like.
CASCADE: list[tuple[int, str, str, str, str, str]] = [
    (
        0,
        "PostgresPrimaryDown",
        "postgres-primary",
        "data",
        "critical",
        "pg_up == 0 for 2m on postgres-primary",
    ),
    (
        8,
        "PostgresConnectionsExhausted",
        "postgres-primary",
        "data",
        "critical",
        "connection pool saturated, 100/100 in use",
    ),
    (14, "ReplicaLagHigh", "postgres-replica", "data", "critical", "replica lag 412s and climbing"),
    (
        19,
        "ReplicaLagHigh",
        "postgres-replica",
        "data",
        "warning",
        "replica lag exceeded warning threshold",
    ),
    (23, "HighErrorRate", "payments-api", "payments", "critical", "5xx rate 41% for payments-api"),
    (
        27,
        "PodCrashLoopBackOff",
        "payments-worker",
        "payments",
        "critical",
        "payments-worker-7d9c4b8f6-x2klm restarting",
    ),
    (31, "HighErrorRate", "payments-worker", "payments", "critical", "worker job failure rate 88%"),
    (
        34,
        "LatencyP99High",
        "payments-api",
        "payments",
        "warning",
        "p99 latency 8.2s (threshold 1s)",
    ),
    (
        38,
        "HighErrorRate",
        "checkout-web",
        "storefront",
        "critical",
        "checkout completion rate dropped to 12%",
    ),
    (41, "LatencyP99High", "checkout-web", "storefront", "warning", "p99 latency 11.4s"),
    (45, "HighErrorRate", "catalog-api", "catalog", "warning", "5xx rate 6% for catalog-api"),
    (49, "LatencyP99High", "catalog-api", "catalog", "warning", "p99 latency 2.1s"),
    (
        53,
        "KafkaConsumerLag",
        "kafka-events",
        "platform",
        "warning",
        "consumer group payments-worker lag 240k",
    ),
    (
        58,
        "KafkaConsumerLag",
        "kafka-events",
        "platform",
        "critical",
        "consumer group notifications lag 890k",
    ),
    (
        62,
        "HighErrorRate",
        "notifications",
        "platform",
        "warning",
        "notification delivery failures 34%",
    ),
    (
        67,
        "SearchIndexStale",
        "search-index",
        "catalog",
        "warning",
        "index lag behind source by 14m",
    ),
    (
        71,
        "PodCrashLoopBackOff",
        "payments-api",
        "payments",
        "critical",
        "payments-api-6f8b restarting, OOMKilled",
    ),
    (76, "PodNotReady", "payments-worker", "payments", "critical", "3/5 replicas not ready"),
    (81, "CacheHitRateLow", "valkey-cache", "platform", "warning", "hit rate dropped to 31%"),
    (
        86,
        "SessionStoreErrors",
        "session-store",
        "storefront",
        "warning",
        "session write failures 12%",
    ),
    (
        92,
        "HighErrorRate",
        "upi-gateway",
        "payments",
        "critical",
        "upstream UPI gateway returning 503",
    ),
    (
        98,
        "PaymentSettlementBacklog",
        "payments-worker",
        "payments",
        "critical",
        "unsettled payments backlog 4,182",
    ),
    (104, "PodNotReady", "checkout-web", "storefront", "warning", "2/6 replicas not ready"),
    (111, "DiskPressure", "postgres-primary", "data", "warning", "WAL volume at 87%"),
    (
        118,
        "PostgresReplicationBroken",
        "postgres-replica",
        "data",
        "critical",
        "replication slot inactive",
    ),
    (125, "HighErrorRate", "session-store", "storefront", "warning", "5xx rate 9%"),
    (132, "LatencyP99High", "payments-worker", "payments", "warning", "job duration p99 47s"),
    (
        139,
        "QueueDepthHigh",
        "kafka-events",
        "platform",
        "warning",
        "topic payments.settlement depth 1.2M",
    ),
    (
        147,
        "PodCrashLoopBackOff",
        "notifications",
        "platform",
        "warning",
        "notifications-5b7c restarting",
    ),
    (
        154,
        "CertificateExpiringSoon",
        "checkout-web",
        "storefront",
        "info",
        "TLS certificate expires in 12 days",
    ),
    (162, "HighErrorRate", "search-index", "catalog", "warning", "indexing failures 22%"),
    (170, "LatencyP99High", "session-store", "storefront", "warning", "p99 latency 890ms"),
    (178, "PodNotReady", "catalog-api", "catalog", "warning", "1/4 replicas not ready"),
    (
        187,
        "MemoryPressure",
        "payments-api",
        "payments",
        "warning",
        "container memory at 91% of limit",
    ),
    (195, "PostgresLocksHigh", "postgres-primary", "data", "warning", "1,204 waiting locks"),
    (204, "HighErrorRate", "payments-api", "payments", "critical", "5xx rate still 38% after 3m"),
    (
        213,
        "SLOBurnRateFast",
        "payments-api",
        "payments",
        "critical",
        "error budget burning at 14.4x",
    ),
    (
        222,
        "SLOBurnRateFast",
        "checkout-web",
        "storefront",
        "critical",
        "error budget burning at 21.7x",
    ),
    (
        231,
        "QueueDepthHigh",
        "kafka-events",
        "platform",
        "warning",
        "topic notifications.outbound depth 640k",
    ),
    (240, "PodNotReady", "notifications", "platform", "warning", "2/3 replicas not ready"),
]


def _alert(offset: int, name: str, service: str, ns: str, sev: str, summary: str) -> dict[str, Any]:
    starts = T0 + timedelta(seconds=offset)
    return {
        "status": "firing",
        "labels": {
            "alertname": name,
            "cluster": CLUSTER,
            "namespace": ns,
            "service": service,
            "severity": sev,
            # Volatile labels, deliberately present: the fingerprint must ignore
            # them, and a fixture without them would never prove that.
            "pod": f"{service}-{abs(hash((name, offset))) % 100000:05d}",
            "instance": f"10.42.{offset % 250}.{(offset * 7) % 250}:9090",
        },
        "annotations": {"summary": summary},
        "startsAt": starts.isoformat().replace("+00:00", "Z"),
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph",
    }


def build(n: int) -> dict[str, Any]:
    entries = CASCADE[:n]
    return {
        "receiver": "incidentpilot",
        "status": "firing",
        "externalURL": "http://alertmanager:9093",
        "version": "4",
        # NO shared groupKey, deliberately.
        #
        # `alertmanager.yml` groups by [alertname, cluster, service], so a
        # cascade across 12 services and 20 alertnames produces many groups, not
        # one -- Alertmanager sends a separate notification per group.
        #
        # An earlier version of this fixture set a single groupKey for all forty
        # alerts. Every alert then shared a dedup_key, attached by exact-identity
        # match, and the correlation scorer never ran: the "40 -> 1 incident"
        # assertion passed without exercising the feature it exists to prove.
        # Omitting it makes dedup_key fall back to the per-alert fingerprint,
        # which is what a finely-grouped Alertmanager actually produces.
        "truncatedAlerts": 0,
        "groupLabels": {"cluster": CLUSTER},
        "commonLabels": {"cluster": CLUSTER},
        "commonAnnotations": {},
        "alerts": [_alert(*e) for e in entries],
    }


def main() -> None:
    for n in (5, 12, 40):
        path = HERE / f"storm_{n}.json"
        path.write_text(json.dumps(build(n), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.name}: {n} alerts")


if __name__ == "__main__":
    main()
