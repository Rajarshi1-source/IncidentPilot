"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { SERIES } from "@/lib/tokens";
import type { ServiceMttr } from "@/lib/zod-schemas";

/**
 * TTM and MTTR side by side, per service.
 *
 * **Kept as two bars rather than one.** Time-to-mitigate is what the responder
 * controls; the tail between mitigation and resolution is paperwork. Averaging
 * them into a single MTTR hides which half is slow, and the half that is slow
 * is the whole question.
 *
 * A service with no resolved incidents in the window is dropped rather than
 * plotted at zero — a bar of height zero reads as "recovers instantly", which
 * is the opposite of "we have no data".
 */
export function MttrChart({ services }: { services: readonly ServiceMttr[] }) {
  const data = services
    .filter((service) => service.p50_mttr_min !== null || service.p50_ttm_min !== null)
    .map((service) => ({
      service: service.service,
      ttm: service.p50_ttm_min ?? 0,
      mttr: service.p50_mttr_min ?? 0,
      p95: service.p95_mttr_min ?? 0,
    }));

  if (data.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-slate-500">
        No service has a resolved incident in this window. Nothing is plotted rather than plotting
        zeros — a zero here would read as instant recovery.
      </p>
    );
  }

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
          <XAxis dataKey="service" tick={{ fontSize: 11 }} interval={0} angle={-20} height={60} textAnchor="end" />
          <YAxis tick={{ fontSize: 11 }} unit="m" />
          <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6 }} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar dataKey="ttm" name="p50 time-to-mitigate" fill={SERIES[0]} radius={[2, 2, 0, 0]} />
          <Bar dataKey="mttr" name="p50 MTTR" fill={SERIES[1]} radius={[2, 2, 0, 0]} />
          <Bar dataKey="p95" name="p95 MTTR" fill={SERIES[2]} radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
