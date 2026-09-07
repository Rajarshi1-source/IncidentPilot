# syntax=docker/dockerfile:1.7
#
# The lifeboat image (W7-18, D7).
#
# Single stage, no uv, no lockfile, no dependency install, and deliberately NOT
# a variant of the bot image. It shares a Python version with the bot and
# nothing else -- not a layer, not a package, not a line of code. If a bad
# dependency resolution takes the bot down, this still starts; if it were built
# `FROM incidentpilot-bot` it would not, and the whole point of a separate
# failure domain would be a comment rather than a property.
#
# Pinned to the digest-stable tag for the same reason every other image here is:
# `latest` in a container file means the thing you reach for during an outage is
# not the thing you tested.

FROM python:3.14.7-slim-trixie

# No pip install. The lifeboat imports json, os, pathlib, sys and urllib, all of
# which ship with the interpreter. An empty requirements file is not a
# simplification here, it is the design: every dependency is another thing that
# can fail to resolve on the worst night of the quarter.

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin lifeboat
WORKDIR /app
COPY main.py /app/main.py

USER 10001

# No HEALTHCHECK: this runs as a CronJob, lives for under a second, and a
# health check on a process whose job is to check health is a joke the operator
# has to untangle at 3 a.m.
ENTRYPOINT ["python", "-u", "/app/main.py"]
