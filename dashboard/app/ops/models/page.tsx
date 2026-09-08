import { read } from "@/lib/api";
import { ModelsResponse } from "@/lib/zod-schemas";
import { Badge } from "@/components/ui/badge";
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
 * Model and prompt provenance (W8-06).
 *
 * The second page that makes this an operator's tool: it answers "which
 * configuration wrote this PIR", and "what did it cost", from a ledger rather
 * than from memory.
 *
 * Cost is grouped by prompt version AND model, because the open question (E-1)
 * is whether the same prompt gets cheaper on a different model — a rollup that
 * collapsed the model could not answer it.
 */
export default async function Models() {
  const models = await read("/api/ops/models?days=60", ModelsResponse);
  if (!models.ok) return <Degraded reason={models.reason} />;

  const { cost, deployments } = models.data;
  const totalSpend = cost.reduce((sum, row) => sum + (row.total_cost_usd ?? 0), 0);
  const totalPirs = cost.reduce((sum, row) => sum + row.pirs, 0);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Models and prompts</h1>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardBody>
            <p className="text-xs text-slate-500">Cost per PIR, last 60 days</p>
            <p className="mt-1 text-3xl font-semibold tabular-nums">
              {totalPirs === 0 ? "—" : `$${(totalSpend / totalPirs).toFixed(4)}`}
            </p>
            <p className="mt-1 text-xs text-slate-500">budget $0.50</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody>
            <p className="text-xs text-slate-500">Model drafts</p>
            <p className="mt-1 text-3xl font-semibold tabular-nums">{totalPirs}</p>
            <p className="mt-1 text-xs text-slate-500">skeletons excluded — they cost nothing</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody>
            <p className="text-xs text-slate-500">Total spend</p>
            <p className="mt-1 text-3xl font-semibold tabular-nums">${totalSpend.toFixed(2)}</p>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Cost by prompt version and model</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {cost.length === 0 ? (
            <Empty>No model drafts yet. Every PIR so far is a deterministic skeleton.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2 font-medium">Prompt</th>
                    <th className="px-4 py-2 font-medium">Model</th>
                    <th className="px-4 py-2 text-right font-medium">PIRs</th>
                    <th className="px-4 py-2 text-right font-medium">Avg cost</th>
                    <th className="px-4 py-2 text-right font-medium">Avg ms</th>
                    <th className="px-4 py-2 text-right font-medium">Avg edit ratio</th>
                  </tr>
                </thead>
                <tbody>
                  {cost.map((row) => (
                    <tr
                      key={`${row.prompt_version}:${row.model}`}
                      className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                    >
                      <td className="px-4 py-2 font-mono text-xs">{row.prompt_version ?? "—"}</td>
                      <td className="px-4 py-2 font-mono text-xs">{row.model ?? "—"}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{row.pirs}</td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {row.avg_cost_usd === null ? "—" : `$${row.avg_cost_usd.toFixed(4)}`}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">{num(row.avg_ms, 0)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {num(row.avg_edit_ratio, 3)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Deployment ledger</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {deployments.length === 0 ? (
            <Empty>No promotions recorded.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2 font-medium">Role</th>
                    <th className="px-4 py-2 font-medium">Model</th>
                    <th className="px-4 py-2 font-medium">State</th>
                    <th className="px-4 py-2 text-right font-medium">Traffic</th>
                    <th className="px-4 py-2 font-medium">Eval run</th>
                    <th className="px-4 py-2 font-medium">Promoted by</th>
                  </tr>
                </thead>
                <tbody>
                  {deployments.map((row) => (
                    <tr
                      key={`${row.role}:${row.model}:${row.started_at}`}
                      className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                    >
                      <td className="px-4 py-2">{row.role}</td>
                      <td className="px-4 py-2 font-mono text-xs">{row.model}</td>
                      <td className="px-4 py-2">
                        <Badge
                          className={
                            row.state === "default"
                              ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                              : "bg-slate-500/15 text-slate-600 dark:text-slate-400"
                          }
                        >
                          {row.state}
                        </Badge>
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">{row.traffic_pct}%</td>
                      <td className="px-4 py-2 font-mono text-xs">{row.eval_run_id ?? "—"}</td>
                      <td className="px-4 py-2 text-xs">{row.promoted_by ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      <p className="text-xs text-slate-500">
        A configuration cannot reach <code>default</code> without an eval run attached — that is a
        check constraint on <code>model_deployments</code>, not a runbook step. The eval harness
        settles a model change by replaying this project&rsquo;s own 40-incident corpus; promoting on
        a price list is how you end up defending a number you did not measure.
      </p>
    </div>
  );
}
