import { read } from "@/lib/api";
import { ActionsResponse, RunbooksResponse } from "@/lib/zod-schemas";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded, Empty } from "@/components/ui/empty";
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
 * Action-item aging and runbook efficacy — postmortems as a control loop.
 *
 * The column that matters is `oldest_open_days`. A median close time looks
 * healthy while one P0 has been open for eight months, and the maximum is what
 * a reviewer actually asks about.
 */
export default async function Actions() {
  const [actions, runbooks] = await Promise.all([
    read("/api/analytics/actions", ActionsResponse),
    read("/api/analytics/runbooks?min_uses=1", RunbooksResponse),
  ]);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Action items and runbooks</h1>

      {!actions.ok ? (
        <Degraded reason={actions.reason} />
      ) : actions.data.priorities.length === 0 ? (
        <Empty>No action items yet. They are extracted from PIRs as those are written.</Empty>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Half-life by priority</CardTitle>
          </CardHeader>
          <CardBody className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2 font-medium">Priority</th>
                    <th className="px-4 py-2 text-right font-medium">Open</th>
                    <th className="px-4 py-2 text-right font-medium">Closed</th>
                    <th className="px-4 py-2 text-right font-medium">Median close (days)</th>
                    <th className="px-4 py-2 text-right font-medium">Oldest open (days)</th>
                  </tr>
                </thead>
                <tbody>
                  {actions.data.priorities.map((row) => (
                    <tr
                      key={row.priority}
                      className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                    >
                      <td className="px-4 py-2 font-medium">{row.priority}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{row.open_now}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{row.closed}</td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {num(row.median_close_days)}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums font-semibold">
                        {num(row.oldest_open_days, 0)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardBody>
        </Card>
      )}

      {runbooks.ok && (
        <Card>
          <CardHeader>
            <CardTitle>Runbook efficacy</CardTitle>
          </CardHeader>
          <CardBody className="p-0">
            {runbooks.data.efficacy.length === 0 ? (
              <p className="p-4 text-sm text-slate-500">
                No runbook has been used yet in a mitigated incident.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                    <tr>
                      <th className="px-4 py-2 font-medium">Runbook</th>
                      <th className="px-4 py-2 text-right font-medium">Uses</th>
                      <th className="px-4 py-2 text-right font-medium">Median TTM (m)</th>
                      <th className="px-4 py-2 text-right font-medium">Adherence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runbooks.data.efficacy.map((row) => (
                      <tr
                        key={row.name}
                        className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                      >
                        <td className="px-4 py-2">{row.name}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{row.uses}</td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {num(row.median_ttm_min)}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {num(row.adherence, 2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {/* The honest caveat, rendered rather than left in a docstring: an
                empty dead-steps list is "not enough data", not a clean bill of
                health, and the loop this feeds has not closed yet (E-3). */}
            <p className="border-t border-slate-200 p-4 text-xs text-slate-500 dark:border-slate-800">
              {runbooks.data.note}
            </p>
          </CardBody>
        </Card>
      )}
    </div>
  );
}
