# syntax=docker/dockerfile:1.7
#
# The dashboard image (W8-03, W8-11).
#
# Node 24 LTS: Node 20 reached end of life in April 2026, and shipping an
# end-of-life runtime in a project whose whole subject is operational rigour is
# the kind of detail a reviewer checks first.
#
# Every tag is pinned. `latest` in a container file means the image you deploy
# during an incident is not the image you tested.

FROM node:24.13.0-trixie-slim AS deps

WORKDIR /app
# The lockfile alone first: dependencies change rarely, source changes on every
# commit, so this ordering keeps the expensive layer cached.
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts


FROM node:24.13.0-trixie-slim AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# The build runs `tsc` and ESLint. A type error fails the image rather than
# shipping — `next build` is this half of the project's gate.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build


FROM node:24.13.0-trixie-slim AS runtime

# HOSTNAME=0.0.0.0 is load-bearing. Next's standalone server otherwise binds to
# the container's own hostname, so the published port works while loopback does
# not -- and the HEALTHCHECK below then fails forever on a container that is
# serving traffic perfectly well.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app

WORKDIR /app

# `output: "standalone"` traces exactly the files the server needs, so this
# copies a few megabytes instead of the whole node_modules tree. Smaller image,
# smaller CVE surface for the Trivy gate to triage.
COPY --from=builder --chown=app:app /app/.next/standalone ./
COPY --from=builder --chown=app:app /app/.next/static ./.next/static

USER 10001
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
