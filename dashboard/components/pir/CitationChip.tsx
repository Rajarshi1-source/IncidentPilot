"use client";

import * as Popover from "@radix-ui/react-popover";
import { CITATION_KIND } from "@/lib/tokens";
import { cn } from "@/lib/utils";

/**
 * One citation, as a chip (W8-05, D1).
 *
 * **The grounding invariant IS the product.** If the UI hides provenance the
 * guarantee is invisible and might as well not exist — so no claim is ever
 * rendered without its chips, and every chip opens the thing it points at.
 *
 * A client component only because of the popover. That is the leaf-level
 * `'use client'` the architecture allows: the claim text, the PIR body and the
 * whole page around it stay server-rendered.
 *
 * Kind-coded because the distinction a reader needs is whether a claim rests on
 * something somebody *said* or something the system *measured*.
 */
export function CitationChip({ kind, refId }: { kind: string; refId: string }) {
  const token = CITATION_KIND[kind] ?? {
    label: kind,
    tone: "bg-slate-500/15 text-slate-700 dark:text-slate-300",
  };

  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          type="button"
          className={cn(
            "ml-1 inline-flex items-center rounded px-1.5 py-0.5 align-middle",
            "font-mono text-[10px] leading-4",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500",
            token.tone,
          )}
          aria-label={`Citation: ${kind} ${refId}`}
        >
          {token.label}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          className="z-50 max-w-sm rounded-md border border-slate-200 bg-white p-3 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-900"
        >
          <p className="font-medium">{kind}</p>
          <p className="mt-1 break-all font-mono text-slate-600 dark:text-slate-400">{refId}</p>
          <p className="mt-2 text-slate-500">
            Every reference here was checked against the evidence actually stored for this
            incident. An ID the validator could not resolve never reached this page.
          </p>
          <Popover.Arrow className="fill-white dark:fill-slate-900" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
