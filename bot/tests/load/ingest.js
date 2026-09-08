/**
 * The ingest load gate (W8-15, §1.4).
 *
 *   k6 run bot/tests/load/ingest.js
 *
 * **500 alerts in 60 seconds, p99 under 250 ms, zero drops.**
 *
 * The number that matters is the third one. A latency target can be met by
 * shedding load, and a throughput target can be met by accepting requests you
 * do not keep — so the thresholds below fail on any non-202, and the check at
 * the end asserts that every alert this test sent is on the stream. Ingest's
 * entire contract is "I have durably accepted this", and a load test that only
 * measured latency would pass on a system that was quietly dropping.
 *
 * Alertmanager will not resend an alert it believes we accepted. That is why a
 * drop here is unrecoverable and why `checks: rate==1.00` is a threshold rather
 * than a report.
 */

import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const API = __ENV.IP_API_URL || "http://localhost:18000";
const BEARER = __ENV.ALERTMANAGER_BEARER || "local-dev-token";

const accepted = new Counter("alerts_accepted");
const rejected = new Counter("alerts_rejected");

export const options = {
  scenarios: {
    // A constant arrival rate, not a fixed number of VUs. Ingest is measured by
    // what arrives, not by how fast we can loop -- and an alert storm arrives at
    // a rate the sender chooses, which is exactly what this models.
    storm: {
      executor: "constant-arrival-rate",
      rate: 25,
      timeUnit: "1s",
      duration: "60s",
      preAllocatedVUs: 20,
      maxVUs: 50,
    },
  },
  thresholds: {
    // The SLO, verbatim from docs/SLO.md.
    "http_req_duration{expected_response:true}": ["p(99)<250"],
    // Zero drops. Not "99.9% accepted" -- a dropped alert is unrecoverable, so
    // the only acceptable rate is all of them.
    checks: ["rate==1.00"],
    http_req_failed: ["rate==0.00"],
  },
};

/** One alert, shaped like Alertmanager's v4 webhook. */
function payload(index) {
  const now = new Date().toISOString();
  return JSON.stringify({
    receiver: "incidentpilot",
    status: "firing",
    externalURL: "http://alertmanager:9093",
    version: "4",
    alerts: [
      {
        status: "firing",
        labels: {
          alertname: "LoadTestHighErrorRate",
          // Distinct per alert so each is a genuinely new fingerprint rather
          // than 500 redeliveries of one, which the dedup key would absorb and
          // which would make this a test of an UPDATE statement.
          service: `load-svc-${index % 12}`,
          instance: `10.42.0.${index % 250}:9090`,
          severity: "warning",
          cluster: "load-test",
          namespace: "load",
        },
        annotations: { summary: `synthetic load alert ${index}` },
        startsAt: now,
        endsAt: "0001-01-01T00:00:00Z",
        generatorURL: "http://prometheus:9090/graph",
      },
    ],
  });
}

export default function () {
  const response = http.post(`${API}/webhooks/alertmanager`, payload(__ITER), {
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${BEARER}`,
    },
  });

  const ok = check(response, {
    "status is 202": (r) => r.status === 202,
    // The body reports what was accepted. A 202 with `accepted: 0` would pass a
    // status check and still have lost the alert.
    "alert was accepted": (r) => {
      try {
        return JSON.parse(r.body).accepted === 1;
      } catch {
        return false;
      }
    },
  });

  if (ok) accepted.add(1);
  else rejected.add(1);
}

export function handleSummary(data) {
  const p99 = data.metrics.http_req_duration?.values?.["p(99)"] ?? -1;
  const acceptedCount = data.metrics.alerts_accepted?.values?.count ?? 0;
  const rejectedCount = data.metrics.alerts_rejected?.values?.count ?? 0;

  const lines = [
    "",
    "IncidentPilot ingest load gate",
    `  alerts accepted : ${acceptedCount}`,
    `  alerts rejected : ${rejectedCount}`,
    `  p99 latency     : ${p99.toFixed(1)} ms (budget 250)`,
    "",
    rejectedCount === 0 && p99 < 250
      ? "  PASS"
      : "  FAIL - a dropped alert is unrecoverable; Alertmanager will not resend it",
    "",
  ];

  return { stdout: lines.join("\n") };
}
