"""The integration suite owns its schema.

**The bug this file exists to kill.** Until now nothing applied migrations for
the integration tests -- the schema appeared as a *side effect* of
``test_storm_integration.py``'s module fixture, which runs ``alembic downgrade
base && alembic upgrade head`` as part of the G2 "from empty" assertion. Every
other integration module quietly depended on that file having run first, and
alphabetically it runs fourth.

It never showed up locally because a developer has already migrated their
database by hand (and ``scripts/gate_g*.sh`` migrate before they run). In CI,
against a fresh service container, ``test_crash_matrix.py`` collided with an
empty database and thirty-eight tests errored with ``relation "incidents" does
not exist``. That step had been red on **every push since week 3** while the
gates and the local suite were green -- which is exactly the shape of failure
this project keeps writing tests about, arriving in the test infrastructure
itself.

The fix is to make the dependency explicit and unordered: migrate once per
session, before anything, so every module is self-sufficient and a single file
can be run in isolation.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

BOT_DIR = Path(__file__).resolve().parents[2]

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)


def _reachable(url: str, *, timeout: float = 2.0) -> bool:
    """Is there a server listening? A plain socket probe, no driver involved.

    Deliberately not an asyncpg/psycopg connection: this runs at session scope
    before any event loop policy is settled, and opening an async connection
    here would be the first thing to break on Windows (see ``runtime.py``).
    """
    parsed = urlparse(url.replace("+psycopg", ""))
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def migrated_schema() -> None:
    """Apply migrations once, before any integration test runs.

    Two behaviours worth stating, because the wrong one in either direction is
    what produced the original bug:

    * **Unreachable database: return quietly.** Each module's own ``sessions``
      fixture already skips with a useful message, and ``test_no_history_calls``
      needs no database at all -- skipping the whole directory from here would
      take that file's INV-02 guards down with it.
    * **Reachable database, failed migration: fail loudly.** A silent pass here
      would hand every downstream module a half-built schema, which is a far
      more confusing failure than the one it replaces.

    ``alembic upgrade head`` rather than ``metadata.create_all``: hypertables,
    compression policies and generated columns exist only in the migrations, so
    ``create_all`` would build a schema that passes tests and does not match
    production.
    """
    if not _reachable(DB_URL):
        return

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BOT_DIR,
        env={**os.environ, "IP_DATABASE_URL": DB_URL},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"integration schema could not be created:\n{result.stdout}\n{result.stderr}")
