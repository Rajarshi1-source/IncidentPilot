/**
 * The read layer. Server Components only (W8-04, W8-07).
 *
 * Caching policy lives here, next to the fetch, rather than at each call site:
 * "how stale may this be?" is a property of the data, and scattering it means
 * two pages eventually disagree about the same endpoint.
 *
 * - **Active incidents: never cached.** A stale banner during an incident is
 *   worse than a slow one.
 * - **Analytics: 300 s.** Nobody is blocked on a ninety-day percentile, and
 *   re-running it per viewer is how a dashboard takes down the database it is
 *   reporting on.
 *
 * Poll, do not stream. A 15-second revalidation is enough for a handful of
 * concurrent incidents; websockets here would be complexity with no payoff, and
 * saying so is a better answer than adding them for show.
 */

import type { ZodType } from "zod";

const API = process.env.IP_API_URL ?? "http://localhost:18000";

/** What a page gets back: the data, or a reason it has none. */
export type Result<T> = { ok: true; data: T } | { ok: false; reason: string };

/**
 * Fetch and validate. Never throws.
 *
 * A page whose analytics endpoint is down must still render its other panels,
 * so a failure here is a value rather than an exception — the caller renders
 * an explicit degraded state. "Metrics unavailable for this window" is a real
 * backend condition (the 503 the read API returns on a statement timeout) and
 * the UI is required to show it rather than a zero.
 */
export async function read<T>(
  path: string,
  schema: ZodType<T>,
  revalidate: number | false = 300,
): Promise<Result<T>> {
  try {
    const response = await fetch(`${API}${path}`, {
      ...(revalidate === false
        ? { cache: "no-store" as const }
        : { next: { revalidate } }),
      headers: { accept: "application/json" },
    });

    if (response.status === 503) {
      return { ok: false, reason: "The query exceeded its time budget. Try again shortly." };
    }
    if (!response.ok) {
      return { ok: false, reason: `The API returned ${response.status}.` };
    }

    const parsed = schema.safeParse(await response.json());
    if (!parsed.success) {
      // Loud on purpose. A schema drift that rendered `undefined` into a chart
      // would look like "no incidents this month", which is a far more
      // expensive thing to believe than an error message.
      console.error(`schema drift on ${path}`, parsed.error.issues);
      return { ok: false, reason: "The API returned a shape this build does not understand." };
    }
    return { ok: true, data: parsed.data };
  } catch (error) {
    return {
      ok: false,
      reason: error instanceof Error ? `Could not reach the API: ${error.message}` : "Unknown error",
    };
  }
}

export const DEMO_MODE = process.env.IP_DEMO_MODE === "true";
export const API_URL = API;
