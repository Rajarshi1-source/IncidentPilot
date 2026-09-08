import { read } from "@/lib/api";
import { ActiveIncidents, TranscriptResponse } from "@/lib/zod-schemas";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded, Empty } from "@/components/ui/empty";
import { severityToken, stateTone } from "@/lib/tokens";
import { duration } from "@/lib/utils";

export const revalidate = 15;

export default async function Incidents() {
  const [incidents, transcript] = await Promise.all([
    read("/api/incidents/active", ActiveIncidents, false),
    read("/api/transcript/completeness", TranscriptResponse, 15),
  ]);

  // Stored counts, keyed by incident. Rendered as a COUNT, never as a ratio:
  // the denominator (what Slack holds) needs a history call we are rate-limited
  // out of, so printing a fraction would imply a number we do not have.
  const stored = new Map(
    transcript.ok ? transcript.data.incidents.map((row) => [row.public_key, row.stored]) : [],
  );

  if (!incidents.ok) return <Degraded reason={incidents.reason} />;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Incidents</h1>
      {incidents.data.incidents.length === 0 ? (
        <Empty>No open incidents.</Empty>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Open</CardTitle>
          </CardHeader>
          <CardBody className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2 font-medium">Key</th>
                    <th className="px-4 py-2 font-medium">Severity</th>
                    <th className="px-4 py-2 font-medium">State</th>
                    <th className="px-4 py-2 font-medium">Service</th>
                    <th className="px-4 py-2 font-medium">Title</th>
                    <th className="px-4 py-2 text-right font-medium">Alerts</th>
                    <th className="px-4 py-2 text-right font-medium">Messages</th>
                    <th className="px-4 py-2 text-right font-medium">Elapsed</th>
                  </tr>
                </thead>
                <tbody>
                  {incidents.data.incidents.map((incident) => {
                    const token = severityToken(incident.severity);
                    return (
                      <tr
                        key={incident.public_key}
                        className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                      >
                        <td className="px-4 py-2 font-mono text-xs">{incident.public_key}</td>
                        <td className="px-4 py-2">
                          <Badge className={token.badge}>{token.label}</Badge>
                        </td>
                        <td className="px-4 py-2">
                          <Badge className={stateTone(incident.state)}>
                            {incident.state.replace(/_/g, " ")}
                          </Badge>
                        </td>
                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400">
                          {incident.service ?? "unassigned"}
                        </td>
                        <td className="max-w-md truncate px-4 py-2">{incident.title}</td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {incident.correlated_alert_count}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {stored.get(incident.public_key) ?? "—"}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {duration(incident.elapsed_min)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </CardBody>
        </Card>
      )}
      <p className="text-xs text-slate-500">
        Messages is what we hold, not a completeness ratio. Slack allows one history call per
        minute, so the denominator is sampled by the reconciler rather than read here (ADR 0003).
      </p>
    </div>
  );
}
