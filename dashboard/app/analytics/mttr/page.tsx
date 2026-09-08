import { read } from "@/lib/api";
import { MttrResponse } from "@/lib/zod-schemas";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded } from "@/components/ui/empty";
import { MttrChart } from "@/components/charts/MttrChart";
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


export default async function Mttr() {
  const mttr = await read("/api/analytics/mttr?days=30", MttrResponse);
  if (!mttr.ok) return <Degraded reason={mttr.reason} />;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Recovery times, last 30 days</h1>

      <Card>
        <CardHeader>
          <CardTitle>Time to mitigate vs time to resolve</CardTitle>
        </CardHeader>
        <CardBody>
          <MttrChart services={mttr.data.services} />
          <p className="mt-3 text-xs text-slate-500">
            TTM is separated from MTTR deliberately. Time-to-mitigate is what the responder
            controls; the tail between mitigation and resolution is paperwork, and averaging the two
            hides which half is slow.
          </p>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Per service</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2 font-medium">Service</th>
                  <th className="px-4 py-2 text-right font-medium">Incidents</th>
                  <th className="px-4 py-2 text-right font-medium">p50 TTA (s)</th>
                  <th className="px-4 py-2 text-right font-medium">p50 TTM (m)</th>
                  <th className="px-4 py-2 text-right font-medium">p50 MTTR (m)</th>
                  <th className="px-4 py-2 text-right font-medium">p95 MTTR (m)</th>
                </tr>
              </thead>
              <tbody>
                {mttr.data.services.map((service) => (
                  <tr
                    key={service.service}
                    className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                  >
                    <td className="px-4 py-2">{service.service}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{service.incidents}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{num(service.p50_tta_s, 0)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{num(service.p50_ttm_min)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{num(service.p50_mttr_min)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{num(service.p95_mttr_min)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>

      <p className="text-xs text-slate-500">
        Incidents with no primary service appear as <code>unassigned</code> rather than being
        dropped. They are usually the ones where triage was hardest and the MTTR worst, and a metric
        that improves by discarding its worst cases is a metric that lies.
      </p>
    </div>
  );
}
