import { cn } from "@/lib/utils";

/**
 * Always renders text, never colour alone.
 *
 * A severity badge that is only red is unreadable to a colourblind responder
 * and invisible in a monochrome screenshot — and this is an operations tool
 * whose screenshots end up in postmortems.
 */
export function Badge({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium",
        className,
      )}
      {...props}
    />
  );
}
