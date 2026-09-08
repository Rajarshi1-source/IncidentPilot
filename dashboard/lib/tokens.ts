/**
 * The one severity and state colour map (W8-08).
 *
 * Used by badges, timeline rows AND charts. One map rather than three, because
 * a Sev1 that is red in a badge and orange in a chart is a dashboard somebody
 * assembled rather than designed — and at 3 a.m. the colour is what people
 * navigate by.
 *
 * Every entry carries a `label` as well as a colour. **Meaning is never encoded
 * in colour alone**: a severity badge reads "Sev1" and a chart series is
 * labelled, so the dashboard works for a colourblind responder and in a
 * screenshot pasted into a monochrome document.
 *
 * Chart colours are raw hex because Recharts renders to SVG attributes and
 * cannot read a CSS variable. Everything else uses the semantic Tailwind
 * classes so dark mode is a token swap rather than a second palette.
 */

export type Severity = "sev1" | "sev2" | "sev3" | "sev4";

export interface Token {
  readonly label: string;
  /** Tailwind classes for a badge. */
  readonly badge: string;
  /** Left border for a timeline row or a banner. */
  readonly rail: string;
  /** Literal colour for Recharts, which cannot read CSS variables. */
  readonly chart: string;
}

export const SEVERITY: Record<Severity, Token> = {
  sev1: {
    label: "Sev1",
    badge: "bg-red-500/15 text-red-700 dark:text-red-300 ring-1 ring-red-500/30",
    rail: "border-l-red-500",
    chart: "#ef4444",
  },
  sev2: {
    label: "Sev2",
    badge: "bg-orange-500/15 text-orange-700 dark:text-orange-300 ring-1 ring-orange-500/30",
    rail: "border-l-orange-500",
    chart: "#f97316",
  },
  sev3: {
    label: "Sev3",
    badge: "bg-amber-500/15 text-amber-700 dark:text-amber-300 ring-1 ring-amber-500/30",
    rail: "border-l-amber-500",
    chart: "#f59e0b",
  },
  sev4: {
    label: "Sev4",
    badge: "bg-slate-500/15 text-slate-700 dark:text-slate-300 ring-1 ring-slate-500/30",
    rail: "border-l-slate-400",
    chart: "#94a3b8",
  },
};

/**
 * The thirteen states, grouped by what a reader needs to know: is this live, is
 * it winding down, or is it over.
 */
export const STATE_TONE: Record<string, string> = {
  detected: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  triaging: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  engaged: "bg-red-500/15 text-red-700 dark:text-red-300",
  acknowledged: "bg-orange-500/15 text-orange-700 dark:text-orange-300",
  mitigated: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  resolved: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  pir_drafting: "bg-violet-500/15 text-violet-700 dark:text-violet-300",
  pir_drafted: "bg-violet-500/15 text-violet-700 dark:text-violet-300",
  pir_failed: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  reopened: "bg-red-500/15 text-red-700 dark:text-red-300",
  closed: "bg-slate-500/15 text-slate-600 dark:text-slate-400",
  merged: "bg-slate-500/15 text-slate-600 dark:text-slate-400",
  false_positive: "bg-slate-500/15 text-slate-600 dark:text-slate-400",
  abandoned: "bg-slate-500/15 text-slate-600 dark:text-slate-400",
};

/**
 * The six citation kinds (D1). Chips are kind-coded so a reader can tell at a
 * glance whether a claim rests on something somebody *said* or on something the
 * system *measured* — which is the distinction the whole grounding guarantee is
 * about.
 */
export const CITATION_KIND: Record<string, { label: string; tone: string }> = {
  message: { label: "msg", tone: "bg-sky-500/15 text-sky-700 dark:text-sky-300" },
  timeline: { label: "timeline", tone: "bg-indigo-500/15 text-indigo-700 dark:text-indigo-300" },
  alert: { label: "alert", tone: "bg-red-500/15 text-red-700 dark:text-red-300" },
  deploy: { label: "deploy", tone: "bg-violet-500/15 text-violet-700 dark:text-violet-300" },
  metric: { label: "metric", tone: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" },
  runbook: { label: "runbook", tone: "bg-amber-500/15 text-amber-700 dark:text-amber-300" },
};

/** Ordered categorical palette for charts with several series. */
export const SERIES = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2"] as const;

export function severityToken(value: string): Token {
  return SEVERITY[value as Severity] ?? SEVERITY.sev4;
}

export function stateTone(value: string): string {
  return STATE_TONE[value] ?? STATE_TONE.closed!;
}
