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
import type { CompressionWeek } from "@/lib/zod-schemas";

/**
 * Alerts ingested versus incidents created — the visual proof of D3.
 *
 * The chart that earns its space: two bars whose ratio is the entire
 * differentiator, and a gap that widens on the weeks a storm happened. A line
 * of the ratio alone would be smaller and would hide *why* it moved.
 *
 * A client component because Recharts needs the browser, receiving
 * pre-aggregated rows. The query layer never ships here — the server has
 * already reduced ninety days to a handful of weekly points.
 *
 * Series carry labels, not just colours: a legend is what makes this readable
 * to a colourblind reader and in a monochrome screenshot.
 */
export function CompressionChart({ weeks }: { weeks: readonly CompressionWeek[] }) {
  const data = weeks.map((week) => ({
    week: week.week.slice(0, 10),
    alerts: week.alerts ?? 0,
    incidents: week.incidents,
  }));

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
          <XAxis dataKey="week" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
          <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6 }} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar dataKey="alerts" name="Alerts ingested" fill={SERIES[0]} radius={[2, 2, 0, 0]} />
          <Bar dataKey="incidents" name="Incidents created" fill={SERIES[1]} radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
