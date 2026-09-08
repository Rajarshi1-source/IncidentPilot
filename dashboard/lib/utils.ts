import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** The shadcn `cn` helper, copied in rather than imported from a package. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Minutes as something a human reads at a glance during an incident. */
export function duration(minutes: number | null): string {
  if (minutes === null) return "—";
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}

/**
 * A number, or an explicit dash.
 *
 * Never `?? 0`. A service with no resolved incidents has no median MTTR, and
 * printing zero would put the service with no data at the top of a "fastest
 * recovery" table.
 */
export function num(value: number | null, digits = 1): string {
  return value === null ? "—" : value.toFixed(digits);
}
