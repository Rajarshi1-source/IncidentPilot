// The skill's mandate: no `any` in app/ or components/.
//
// A grep rather than an ESLint rule, deliberately. `@typescript-eslint` is not
// exposed at the top level of `eslint-config-next`'s flat config, and reaching
// into its internals to add one rule would be a config that breaks on the next
// minor. This is the same shape as the INV-07 check on the Python side: a
// mechanical assertion that reads in one line and cannot silently stop working.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOTS = ["app", "components", "lib"];
const PATTERN = /(:\s*any\b|<any>|as\s+any\b)/;

function walk(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

const offenders = ROOTS.flatMap(walk)
  .filter((file) => /\.tsx?$/.test(file))
  .flatMap((file) =>
    readFileSync(file, "utf8")
      .split("\n")
      .map((line, index) => ({ file, line: index + 1, text: line }))
      .filter(({ text }) => PATTERN.test(text)),
  );

if (offenders.length > 0) {
  for (const { file, line, text } of offenders) {
    console.error(`${file}:${line}: ${text.trim()}`);
  }
  console.error("\n`any` at the API boundary makes the Zod schemas decoration.");
  process.exit(1);
}
console.log(`no \`any\` in ${ROOTS.join(", ")}`);
