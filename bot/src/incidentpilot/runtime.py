"""Event-loop selection.

psycopg's async mode does not work on Windows' default ``ProactorEventLoop``:

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in
    async mode. Please use a compatible event loop, for instance by running
    'asyncio.run(..., loop_factory=asyncio.SelectorEventLoop(...))'

Production runs in Linux containers where the default is already a selector
loop, so this is purely a local-development concern -- but it is the worst shape
of bug to leave unhandled, because the suite passes in CI and every database
call fails on the developer's machine.

Import this before anything opens an async connection.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop() -> None:
    """Install a psycopg-compatible loop policy on Windows. No-op elsewhere."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
