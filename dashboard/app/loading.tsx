/** A skeleton, not a spinner: a spinner says "wait", a skeleton says "wait, and
    here is the shape of what is coming". */
export default function Loading() {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="Loading">
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-20 animate-pulse rounded-lg bg-slate-200 dark:bg-slate-800" />
      ))}
    </div>
  );
}
