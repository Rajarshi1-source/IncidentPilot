import { Card, CardBody } from "@/components/ui/card";

/**
 * The empty state and the degraded state are different things and look
 * different.
 *
 * "No incidents in this window" is good news. "Metrics unavailable for this
 * window" is a backend condition the reader must not mistake for a zero — the
 * whole impact subsystem exists to keep that distinction, and the UI would
 * throw it away by rendering both as a blank panel.
 */
export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <Card>
      <CardBody className="py-10 text-center text-sm text-slate-500 dark:text-slate-400">
        {children}
      </CardBody>
    </Card>
  );
}

export function Degraded({ reason }: { reason: string }) {
  return (
    <Card className="border-amber-300 dark:border-amber-800">
      <CardBody className="py-6 text-sm">
        <p className="font-medium text-amber-700 dark:text-amber-300">Data unavailable</p>
        <p className="mt-1 text-slate-600 dark:text-slate-400">{reason}</p>
        <p className="mt-2 text-xs text-slate-500">
          This panel is showing nothing rather than showing a zero. A zero here would be a
          measurement; this is an absence.
        </p>
      </CardBody>
    </Card>
  );
}
