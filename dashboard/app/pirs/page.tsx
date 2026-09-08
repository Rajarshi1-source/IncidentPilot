import { read } from "@/lib/api";
import { GroundingResponse } from "@/lib/zod-schemas";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Degraded, Empty } from "@/components/ui/empty";
import { Claim } from "@/components/pir/Claim";
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
 * The PIR list, with the two columns almost nobody publishes: citation coverage
 * and human edit ratio.
 *
 * A PIR view that shows how much a human had to rewrite is unusually honest —
 * it is the only number on this dashboard that can make the AI feature look
 * bad, which is exactly why it is here.
 */
export default async function Pirs() {
  const grounding = await read("/api/ops/grounding?limit=30", GroundingResponse);
  if (!grounding.ok) return <Degraded reason={grounding.reason} />;

  const days = grounding.data.days;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold tracking-tight">Post-incident reviews</h1>

      <Card>
        <CardHeader>
          <CardTitle>What a grounded claim looks like</CardTitle>
        </CardHeader>
        <CardBody className="space-y-2 text-sm">
          {/* Rendered from a literal rather than fetched: this panel exists to
              show the reader what a citation chip IS, on a page they may land
              on before any PIR has been written. */}
          <Claim
            claim={{
              text: "The replica was promoted after the primary lost its lease.",
              citations: [
                { kind: "message", ref: "msg:1757000000.000100" },
                { kind: "alert", ref: "alert:f7140e304badf8794c1e428cebbe3ac2" },
              ],
            }}
          />
          <p className="text-xs text-slate-500">
            Every claim in a published PIR carries at least one citation. That is a schema
            constraint rather than a prompt instruction, so an uncited claim cannot be constructed
            at all. Click a chip to see what it resolves to.
          </p>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Daily grounding compliance</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {days.length === 0 ? (
            <Empty>No PIRs written yet.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2 font-medium">Day</th>
                    <th className="px-4 py-2 text-right font-medium">PIRs</th>
                    <th className="px-4 py-2 text-right font-medium">Fully grounded</th>
                    <th className="px-4 py-2 text-right font-medium">Skeletons</th>
                    <th className="px-4 py-2 text-right font-medium">Avg edit ratio</th>
                  </tr>
                </thead>
                <tbody>
                  {days.map((day) => (
                    <tr
                      key={day.day}
                      className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                    >
                      <td className="px-4 py-2 font-mono text-xs">{day.day.slice(0, 10)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{day.pirs}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{day.fully_grounded}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{day.skeletons}</td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {num(day.avg_edit_ratio, 3)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
