/**
 * Zod at the API boundary (W8-07).
 *
 * **The dashboard must fail loudly on schema drift, not render `undefined` into
 * a chart.** A backend field rename is a bug; a chart that silently plots
 * nothing is the same bug, discovered a week later by somebody who assumed the
 * incident rate had dropped.
 *
 * Every response is parsed here and the inferred types are exported, so there is
 * exactly one definition of what an incident looks like on this side of the
 * wire. Nothing downstream writes `any`.
 */

import { z } from "zod";

/** A number the backend may legitimately not have. See the `null` note below. */
const nullableNumber = z.number().nullable();

export const ActiveIncident = z.object({
  public_key: z.string(),
  title: z.string(),
  severity: z.string(),
  state: z.string(),
  service: z.string().nullable(),
  // Both, deliberately. `elapsed_min` server-renders the first paint;
  // `detected_at` is what the client timer counts from, so the tick never
  // depends on clock arithmetic against a rendered string.
  detected_at: z.string(),
  elapsed_min: z.number(),
  correlated_alert_count: z.number(),
  chat_channel_id: z.string().nullable(),
});
export type ActiveIncident = z.infer<typeof ActiveIncident>;

export const ActiveIncidents = z.object({ incidents: z.array(ActiveIncident) });

/**
 * `null` is a first-class value throughout these schemas, never coerced to 0.
 *
 * A service with no resolved incidents in the window has no median MTTR — that
 * is not zero minutes, and rendering it as zero would put the healthiest
 * service at the top of a "fastest recovery" chart. The UI renders these as
 * an explicit dash.
 */
export const ServiceMttr = z.object({
  service: z.string(),
  incidents: z.number(),
  p50_tta_s: nullableNumber,
  p50_ttm_min: nullableNumber,
  p50_mttr_min: nullableNumber,
  p95_mttr_min: nullableNumber,
});
export type ServiceMttr = z.infer<typeof ServiceMttr>;

export const MttrResponse = z.object({ days: z.number(), services: z.array(ServiceMttr) });

export const CompressionWeek = z.object({
  week: z.string(),
  incidents: z.number(),
  alerts: nullableNumber,
  ratio: nullableNumber,
});
export type CompressionWeek = z.infer<typeof CompressionWeek>;

export const CompressionResponse = z.object({ days: z.number(), weeks: z.array(CompressionWeek) });

export const ActionPriority = z.object({
  priority: z.string(),
  open_now: z.number(),
  closed: z.number(),
  median_close_days: nullableNumber,
  oldest_open_days: nullableNumber,
});
export type ActionPriority = z.infer<typeof ActionPriority>;

export const ActionsResponse = z.object({ priorities: z.array(ActionPriority) });

export const GroundingDay = z.object({
  day: z.string(),
  pirs: z.number(),
  fully_grounded: z.number(),
  skeletons: z.number(),
  avg_edit_ratio: nullableNumber,
});
export type GroundingDay = z.infer<typeof GroundingDay>;

export const GroundingResponse = z.object({ days: z.array(GroundingDay) });

export const CostRow = z.object({
  prompt_version: z.string().nullable(),
  model: z.string().nullable(),
  pirs: z.number(),
  avg_cost_usd: nullableNumber,
  total_cost_usd: nullableNumber,
  avg_ms: nullableNumber,
  avg_edit_ratio: nullableNumber,
});
export type CostRow = z.infer<typeof CostRow>;

export const Deployment = z.object({
  role: z.string(),
  provider: z.string(),
  model: z.string(),
  state: z.string(),
  traffic_pct: z.number(),
  prompt_version: z.string().nullable(),
  eval_run_id: z.string().nullable(),
  promoted_by: z.string().nullable(),
  started_at: z.string(),
  ended_at: z.string().nullable(),
});
export type Deployment = z.infer<typeof Deployment>;

export const ModelsResponse = z.object({
  days: z.number(),
  cost: z.array(CostRow),
  deployments: z.array(Deployment),
});

export const TranscriptRow = z.object({
  public_key: z.string(),
  chat_channel_id: z.string().nullable(),
  stored: z.number(),
  newest_stored_at: z.string().nullable(),
});
export const TranscriptResponse = z.object({ incidents: z.array(TranscriptRow) });

export const RunbookEfficacy = z.object({
  name: z.string(),
  uses: z.number(),
  median_ttm_min: nullableNumber,
  adherence: nullableNumber,
});
export const DeadStep = z.object({
  name: z.string(),
  step: z.string(),
  uses: z.number(),
  skipped: z.number(),
  skip_rate: nullableNumber,
});
export const RunbooksResponse = z.object({
  efficacy: z.array(RunbookEfficacy),
  dead_steps: z.array(DeadStep),
  dead_step_min_uses: z.number(),
  note: z.string(),
});
