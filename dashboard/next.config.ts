import type { NextConfig } from "next";

const config: NextConfig = {
  // A single self-contained bundle, so the container image carries the app and
  // not 400 MB of node_modules. The Helm chart and compose both run this.
  output: "standalone",
  reactStrictMode: true,
  // Fail the build on a type error rather than shipping it. `next build` is the
  // dashboard's gate, and a build that ignores TypeScript proves nothing.
  typescript: { ignoreBuildErrors: false },
};

export default config;
