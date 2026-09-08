import { read } from "@/lib/api";
import { ActiveIncidents, CompressionResponse, GroundingResponse } from "@/lib/zod-schemas";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded, Empty } from "@/components/ui/empty";
import { LiveTimer } from "@/components/LiveTimer";
import { severityToken, stateTone } from "@/lib/tokens";
import { num } from "@/lib/utils";

// Poll, do not stream. Fifteen seconds is enough for a handful of concurrent
// incidents and costs nothing operationally; websockets here would be
// complexity with no payoff.
export const revalidate = 15;

/**
 * The active-incident banner (W8-04).
 *
 * One row per incident, not a card grid: during a real incident nobody reads a
 * card grid. Severity, elapsed, who is on it, how many alerts it absorbed, and
 * a way into Slack — in the order a responder scans them.
 *
 * A Server Component. Everything here is a data read; the only thing that ships
 * to the browser is the timer.
 */
export default async function Home() {
  const [active, compression, grounding] = await Promise.all([
    read("/api/incidents/active", ActiveIncidents, false),
    read("/api/analytics/compression?days=90", CompressionResponse),
    read("/api/ops/grounding?limit=7", GroundingResponse),
  ]);

  return (
    <div className="space-y-6">
      <section>
        <h1 className="mb-3 text-lg font-semibold tracking-tight">Active incidents</h1>
        {!active.ok ? (
          <Degraded reason={active.reason} />
        ) : active.data.incidents.length === 0 ? (
          <Empty>
            Nothing is on fire. Incidents appear here the moment an alert is correlated — this
            page never caches.
          </Empty>
        ) : (
          <ul className="space-y-2">
            {active.data.incidents.map((incident) => {
              const token = severityToken(incident.severity);
              return (
                <li key={incident.public_key}>
                  <Card className={`border-l-4 ${token.rail}`}>
                    <CardBody className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3">
                      <Badge className={token.badge}>{token.label}</Badge>
                      <Badge className={stateTone(incident.state)}>
                        {incident.state.replace(/_/g, " ")}
                      </Badge>

                      <span className="min-w-0 flex-1 truncate text-sm font-medium">
                        {incident.title}
                      </span>

                      <span className="text-xs text-slate-500 dark:text-slate-400">
                        {incident.service ?? "unassigned"}
                      </span>

                      <span
                        className="text-xs text-slate-600 dark:text-slate-400"
                        title="Alerts absorbed into this one incident (D3)"
                      >
                        {incident.correlated_alert_count} alerts
                      </span>

                      <span className="text-sm">
                        <LiveTimer
                          detectedAt={incident.detected_at}
                          serverElapsedMin={incident.elapsed_min}
                        />
                      </span>

                      {incident.chat_channel_id && (
                        <span className="font-mono text-xs text-slate-500">
                          {incident.chat_channel_id}
                        </span>
                      )}
                    </CardBody>
                  </Card>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Storm compression, last 90 days</CardTitle>
          </CardHeader>
          <CardBody>
            {!compression.ok ? (
              <p className="text-sm text-amber-700 dark:text-amber-300">{compression.reason}</p>
            ) : compression.data.weeks.length === 0 ? (
              <p className="text-sm text-slate-500">No incidents in this window yet.</p>
            ) : (
              <dl className="grid grid-cols-3 gap-4 text-sm">
                <div>
                  <dt className="text-xs text-slate-500">Alerts ingested</dt>
                  <dd className="mt-1 text-2xl font-semibold tabular-nums">
                    {compression.data.weeks.reduce((sum, w) => sum + (w.alerts ?? 0), 0)}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Incidents created</dt>
                  <dd className="mt-1 text-2xl font-semibold tabular-nums">
                    {compression.data.weeks.reduce((sum, w) => sum + w.incidents, 0)}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Compression</dt>
                  <dd className="mt-1 text-2xl font-semibold tabular-nums text-emerald-600 dark:text-emerald-400">
                    {(() => {
                      const alerts = compression.data.weeks.reduce((s, w) => s + (w.alerts ?? 0), 0);
                      const incidents = compression.data.weeks.reduce((s, w) => s + w.incidents, 0);
                      return incidents === 0 ? "—" : `${(alerts / incidents).toFixed(1)}×`;
                    })()}
                  </dd>
                </div>
              </dl>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>PIR grounding, last 7 days</CardTitle>
          </CardHeader>
          <CardBody>
            {!grounding.ok ? (
              <p className="text-sm text-amber-700 dark:text-amber-300">{grounding.reason}</p>
            ) : grounding.data.days.length === 0 ? (
              <p className="text-sm text-slate-500">No PIRs written yet.</p>
            ) : (
              (() => {
                const pirs = grounding.data.days.reduce((s, d) => s + d.pirs, 0);
                const grounded = grounding.data.days.reduce((s, d) => s + d.fully_grounded, 0);
                const skeletons = grounding.data.days.reduce((s, d) => s + d.skeletons, 0);
                const drafted = pirs - skeletons;
                return (
                  <dl className="grid grid-cols-3 gap-4 text-sm">
                    <div>
                      <dt className="text-xs text-slate-500">Model drafts</dt>
                      <dd className="mt-1 text-2xl font-semibold tabular-nums">{drafted}</dd>
                    </div>
                    <div>
                      <dt className="text-xs text-slate-500">Fully grounded</dt>
                      <dd className="mt-1 text-2xl font-semibold tabular-nums">
                        {drafted === 0 ? "—" : `${num((grounded / drafted) * 100, 1)}%`}
                      </dd>
                    </div>
                    <div>
                      {/* Reported next to the ratio rather than folded into it.
                          A skeleton makes no cited claims, so counting it as a
                          grounding failure would make the flagship metric
                          unreadable on exactly the days the provider was down. */}
                      <dt className="text-xs text-slate-500">Skeletons</dt>
                      <dd className="mt-1 text-2xl font-semibold tabular-nums">{skeletons}</dd>
                    </div>
                  </dl>
                );
              })()
            )}
            <p className="mt-3 text-xs text-slate-500">
              Grounding has a zero error budget: it is an invariant enforced by a deterministic
              validator, not a target. Skeletons are counted separately — they make no cited claims.
            </p>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
