import { read } from "@/lib/api";
import { CompressionResponse } from "@/lib/zod-schemas";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded, Empty } from "@/components/ui/empty";
import { CompressionChart } from "@/components/charts/CompressionChart";
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


/** Alert-storm compression (D3) — the differentiator, made visible. */
export default async function Correlation() {
  const compression = await read("/api/analytics/compression?days=90", CompressionResponse);
  if (!compression.ok) return <Degraded reason={compression.reason} />;

  const weeks = compression.data.weeks;
  const alerts = weeks.reduce((sum, week) => sum + (week.alerts ?? 0), 0);
  const incidents = weeks.reduce((sum, week) => sum + week.incidents, 0);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Alert-storm compression</h1>

      {weeks.length === 0 ? (
        <Empty>No incidents in the last 90 days.</Empty>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Alerts ingested vs incidents created</CardTitle>
            </CardHeader>
            <CardBody>
              <CompressionChart weeks={weeks} />
              <p className="mt-3 text-xs text-slate-500">
                The gap is the product. Forty alerts from one cascade become one war room, and
                every merge records the score and the reasons that produced it — explainable
                grouping is the difference from a commercial tool that just says &ldquo;related&rdquo;.
              </p>
            </CardBody>
          </Card>

          <div className="grid gap-4 md:grid-cols-3">
            <Card>
              <CardBody>
                <p className="text-xs text-slate-500">Alerts ingested</p>
                <p className="mt-1 text-3xl font-semibold tabular-nums">{alerts}</p>
              </CardBody>
            </Card>
            <Card>
              <CardBody>
                <p className="text-xs text-slate-500">Incidents created</p>
                <p className="mt-1 text-3xl font-semibold tabular-nums">{incidents}</p>
              </CardBody>
            </Card>
            <Card>
              <CardBody>
                <p className="text-xs text-slate-500">Compression ratio</p>
                <p className="mt-1 text-3xl font-semibold tabular-nums text-emerald-600 dark:text-emerald-400">
                  {incidents === 0 ? "—" : `${num(alerts / incidents)}×`}
                </p>
              </CardBody>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>By week</CardTitle>
            </CardHeader>
            <CardBody className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                    <tr>
                      <th className="px-4 py-2 font-medium">Week</th>
                      <th className="px-4 py-2 text-right font-medium">Alerts</th>
                      <th className="px-4 py-2 text-right font-medium">Incidents</th>
                      <th className="px-4 py-2 text-right font-medium">Ratio</th>
                    </tr>
                  </thead>
                  <tbody>
                    {weeks.map((week) => (
                      <tr
                        key={week.week}
                        className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                      >
                        <td className="px-4 py-2 font-mono text-xs">{week.week.slice(0, 10)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{week.alerts ?? "—"}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{week.incidents}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{num(week.ratio)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
}
