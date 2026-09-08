import { read } from "@/lib/api";
import { GroundingResponse, TranscriptResponse } from "@/lib/zod-schemas";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded } from "@/components/ui/empty";
import { num } from "@/lib/utils";

/**
 * Rendered per request, never prerendered at build time.
 *
 * Without this Next evaluates the page during `docker build`, when no API
 * exists — and bakes the resulting "could not reach the API" into the image.
 * The container then serves that error until the first revalidation, which is
 * a broken dashboard on every fresh deploy.
 *
 * Caching still happens, one level down: `read()` passes `next: { revalidate }`
 * so the *data* is cached for five minutes even though the shell is dynamic.
 * That is the combination this deployment needs — no build-time fetch, no
 * per-viewer database scan.
 */
export const dynamic = "force-dynamic";


/**
 * The platform's own SLOs (W8-06).
 *
 * One of the two pages that make this an operator's tool rather than a CRUD
 * app. Most people build an SRE tool and never define its SLIs; this page is
 * the argument that IncidentPilot is production infrastructure and knows what
 * it owes.
 *
 * Two of these deserve the space they take:
 *
 * - **Transcript completeness is the real availability metric.** The bot can be
 *   100% up and still have silently dropped three messages, after which the PIR
 *   is wrong and nobody notices. Availability of a data-collection system is
 *   measured in data, not HTTP 200s.
 * - **Grounding has a zero error budget**, because it is an invariant enforced
 *   by a deterministic validator rather than a probabilistic target. Every other
 *   row here is a percentage; that one is a yes or no.
 */

const SLOS = [
  { sli: "Ingest availability", definition: "1 − (5xx on /webhooks/* ÷ total)", target: "99.9%", budget: "43 min / 30 d" },
  { sli: "Ingest latency", definition: "p99 webhook ack", target: "< 250 ms", budget: "—" },
  { sli: "Time-to-war-room", definition: "alert accepted → channel + responder + runbook", target: "p95 < 10 s", budget: "—" },
  { sli: "Responder notified", definition: "alert accepted → page delivered, or the channel told nobody was", target: "p95 < 30 s", budget: "—" },
  { sli: "Transcript completeness", definition: "of Slack's newest page, the fraction we hold (sampled, ADR 0003)", target: "≥ 99.99%", budget: "see note" },
  { sli: "PIR delivery", definition: "resolve → draft posted, any layer including skeleton", target: "99.5%, p95 < 90 s", budget: "3.6 h / 30 d" },
  { sli: "PIR grounding", definition: "PIRs with zero uncited claims", target: "100% — hard invariant", budget: "zero" },
  { sli: "Cost per incident", definition: "LLM spend ÷ incidents", target: "< $0.50", budget: "breaker at 2×" },
] as const;

export default async function Slo() {
  const [grounding, transcript] = await Promise.all([
    read("/api/ops/grounding?limit=30", GroundingResponse, 60),
    read("/api/transcript/completeness", TranscriptResponse, 60),
  ]);

  const drafted = grounding.ok
    ? grounding.data.days.reduce((sum, day) => sum + day.pirs - day.skeletons, 0)
    : 0;
  const groundedCount = grounding.ok
    ? grounding.data.days.reduce((sum, day) => sum + day.fully_grounded, 0)
    : 0;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Platform SLOs</h1>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>PIR grounding — zero error budget</CardTitle>
          </CardHeader>
          <CardBody>
            {!grounding.ok ? (
              <Degraded reason={grounding.reason} />
            ) : (
              <>
                <p className="text-3xl font-semibold tabular-nums">
                  {drafted === 0 ? "—" : `${num((groundedCount / drafted) * 100, 2)}%`}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {groundedCount} of {drafted} model drafts fully grounded, last 30 days.
                  {drafted === 0 && " No model drafts yet — every PIR so far is a skeleton."}
                </p>
                <p className="mt-2 text-xs text-slate-500">
                  A dip is an incident, not a trend. This is enforced by a deterministic validator
                  before publication, so the number is 100% or something is broken.
                </p>
              </>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Transcript completeness</CardTitle>
          </CardHeader>
          <CardBody>
            {!transcript.ok ? (
              <Degraded reason={transcript.reason} />
            ) : (
              <>
                <p className="text-3xl font-semibold tabular-nums">
                  {transcript.data.incidents.reduce((sum, row) => sum + row.stored, 0)}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  messages stored across {transcript.data.incidents.length} open incident(s)
                </p>
                <p className="mt-2 text-xs text-slate-500">
                  A count, not a ratio. The denominator — what Slack holds — needs a history call
                  we are rate-limited out of, so the reconciler samples it separately rather than
                  this page implying a number it does not have.
                </p>
              </>
            )}
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>The eight SLIs</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2 font-medium">SLI</th>
                  <th className="px-4 py-2 font-medium">Definition</th>
                  <th className="px-4 py-2 font-medium">Target</th>
                  <th className="px-4 py-2 font-medium">Error budget</th>
                </tr>
              </thead>
              <tbody>
                {SLOS.map((slo) => (
                  <tr
                    key={slo.sli}
                    className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                  >
                    <td className="px-4 py-2 font-medium">{slo.sli}</td>
                    <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{slo.definition}</td>
                    <td className="px-4 py-2 tabular-nums">{slo.target}</td>
                    <td className="px-4 py-2 tabular-nums">{slo.budget}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>

      <p className="text-xs text-slate-500">
        These were written in week 1, before the code that measures them. Burn-rate alerting is in{" "}
        <code>monitoring/prometheus/rules/incidentpilot-slo.yml</code> — two windows on every
        objective, because a single threshold on a 99.9% target either pages constantly or never
        fires. <code>IncidentPilotDown</code> deliberately does not route through IncidentPilot.
      </p>
    </div>
  );
}
