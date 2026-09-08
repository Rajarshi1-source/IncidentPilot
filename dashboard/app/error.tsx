"use client";

export default function Error({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="rounded-lg border border-red-300 p-6 dark:border-red-800">
      <h2 className="text-sm font-semibold text-red-700 dark:text-red-300">This page failed</h2>
      <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{error.message}</p>
      <button
        onClick={reset}
        className="mt-3 rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
      >
        Try again
      </button>
    </div>
  );
}
