"use client";

import { useEffect, useState } from "react";

/**
 * The elapsed clock on an active incident.
 *
 * **It ticks from a server-supplied instant, never from client clock
 * arithmetic against a rendered string.** Two reasons, and the second is the
 * one that bites:
 *
 * 1. A rendered "14m" is already stale by the time it paints, and parsing it
 *    back into a number to add seconds to compounds the error every tick.
 * 2. The viewer's clock is not the server's. A laptop three minutes fast would
 *    show every incident as three minutes older than it is — and during an
 *    incident that number is the one people quote to each other.
 *
 * So the server sends `detected_at`, this measures the *offset* between the
 * two clocks once on mount, and every tick renders `serverNow - detectedAt`.
 * Skew cancels out.
 *
 * `suppressHydrationWarning` is deliberate: the server renders the elapsed time
 * at request time and the client re-renders it a moment later, so the two
 * genuinely differ. That is correct behaviour for a clock, and the alternative
 * — rendering nothing until mount — makes the most important number on the page
 * flash in late.
 */
export function LiveTimer({
  detectedAt,
  serverElapsedMin,
}: {
  detectedAt: string;
  /** What the server computed at render time; used to derive the clock offset. */
  serverElapsedMin: number;
}) {
  const detected = new Date(detectedAt).getTime();

  // The server's view of "now", reconstructed from the elapsed figure it sent.
  // Mounting is the only moment the two clocks are comparable.
  const [offsetMs] = useState(() => detected + serverElapsedMin * 60_000 - Date.now());
  const [now, setNow] = useState(() => Date.now() + offsetMs);

  useEffect(() => {
    // One second is enough. A sub-second timer on an incident that has been
    // running for forty minutes is precision nobody reads, paid for in wakeups.
    const id = setInterval(() => setNow(Date.now() + offsetMs), 1000);
    return () => clearInterval(id);
  }, [offsetMs]);

  const seconds = Math.max(0, Math.floor((now - detected) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;

  return (
    <time
      dateTime={detectedAt}
      suppressHydrationWarning
      className="font-mono tabular-nums"
      title={`Detected at ${detectedAt}`}
    >
      {h > 0 ? `${h}:${String(m).padStart(2, "0")}` : m}
      {h > 0 ? "" : ":"}
      {h > 0 ? `:${String(s).padStart(2, "0")}` : String(s).padStart(2, "0")}
    </time>
  );
}
