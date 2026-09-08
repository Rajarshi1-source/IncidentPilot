import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { DEMO_MODE } from "@/lib/api";

export const metadata: Metadata = {
  title: "IncidentPilot",
  description: "Incident response with citation-anchored post-incident reviews",
};

const NAV = [
  { href: "/", label: "Active" },
  { href: "/incidents", label: "Incidents" },
  { href: "/pirs", label: "PIRs" },
  { href: "/analytics/mttr", label: "MTTR" },
  { href: "/analytics/correlation", label: "Compression" },
  { href: "/analytics/actions", label: "Actions" },
  { href: "/ops/slo", label: "SLO" },
  { href: "/ops/models", label: "Models" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body>
        {/* The demo banner is not decoration. Every incident on a demo
            deployment is synthetic, and a portfolio dashboard that presented
            invented numbers as production data would be exactly the dishonesty
            the PIR subsystem spends six weeks preventing. */}
        {DEMO_MODE && (
          <div
            role="status"
            className="bg-amber-500/15 px-4 py-2 text-center text-xs text-amber-800 dark:text-amber-200"
          >
            <strong>Demo mode.</strong> Every incident below is synthetic seeded data. This
            deployment cannot create a Slack channel, page anyone, or call a model — the adapters
            resolve to in-memory fakes.
          </div>
        )}

        <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
            <Link href="/" className="text-sm font-semibold tracking-tight">
              IncidentPilot
            </Link>
            <nav aria-label="Main" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>
        </header>

        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
