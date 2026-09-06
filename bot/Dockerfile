# syntax=docker/dockerfile:1.7
#
# Multi-stage, non-root, reproducible. Every layer is pinned; `latest` in a
# container file means your reproduction of last Tuesday's bug runs on a
# different system than last Tuesday did.

FROM python:3.14.7-slim-trixie AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source: the lockfile changes rarely, the source changes on
# every commit, so this ordering keeps the expensive layer cached.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.14.7-slim-trixie AS runtime

# curl is here only for HEALTHCHECK. It is a deliberate, single addition -- every
# extra package in a runtime image is another CVE to triage in the Trivy gate.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IP_ENVIRONMENT=prod

USER 10001

EXPOSE 8000

# Liveness only -- /healthz deliberately checks no dependencies, so a Valkey
# blip cannot make Docker or Kubernetes kill a healthy container.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

# Two workers, not more: this container is I/O bound on webhook handling, and
# the orchestration that would use CPU runs in the worker deployment.
ENTRYPOINT ["uvicorn", "incidentpilot.main:app", \
            "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
