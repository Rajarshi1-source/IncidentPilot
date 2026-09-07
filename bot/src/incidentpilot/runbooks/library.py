"""Load the runbooks once, keep their database ids to hand.

A fifth module beyond the plan's ``{parser, matcher, renderer, detector}``, and
the reason is that none of those four owns this responsibility. Parsing is pure,
matching is pure, rendering is pure -- but *"the eight files on disk, synced to
the ``runbooks`` table, addressable by the id that ``runbook_step_signals``
references"* is stateful, and hiding it inside one of the pure modules would be
the thing that makes them impure.

The sync is idempotent and runs at startup. Runbooks are files in git, not rows
someone edits in a database: the file is the source of truth, and the table
exists so that ``incidents.runbook_id`` and ``runbook_step_signals.runbook_id``
have something to reference.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.db import repositories as repo
from incidentpilot.runbooks.matcher import RunbookMatch, match
from incidentpilot.runbooks.parser import ParsedRunbook, load_runbooks
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

# `bot/src/incidentpilot/runbooks/` -> repo root. The runbooks live beside the
# bot package, not inside it: they are operational documentation that people
# edit in a pull request, not application data.
REPO_ROOT = Path(__file__).resolve().parents[4]


def resolve_dir(directory: Path | str) -> Path:
    """Resolve ``runbooks_dir`` against the repo, not against the working directory.

    ``IP_RUNBOOKS_DIR`` defaults to the relative ``runbooks``, and a relative
    path means something different to the API (started in ``bot/``), the relay
    (started anywhere) and a container (``/app``). Trying the literal path first
    keeps an absolute override working; falling back to the repo root is what
    makes the default correct from every one of those three places.
    """
    candidate = Path(directory)
    if candidate.is_dir():
        return candidate
    fallback = REPO_ROOT / candidate
    return fallback if fallback.is_dir() else candidate


class RunbookLibrary:
    """The parsed runbooks plus their row ids."""

    def __init__(self, runbooks: list[ParsedRunbook], ids: dict[str, int] | None = None) -> None:
        self._runbooks = runbooks
        self._ids = ids or {}

    @classmethod
    def from_directory(cls, directory: Path | str) -> RunbookLibrary:
        return cls(load_runbooks(resolve_dir(directory)))

    @property
    def runbooks(self) -> list[ParsedRunbook]:
        return self._runbooks

    def id_for(self, name: str) -> int | None:
        return self._ids.get(name)

    def by_id(self, runbook_id: int) -> ParsedRunbook | None:
        for name, value in self._ids.items():
            if value == runbook_id:
                return self.by_name(name)
        return None

    def by_name(self, name: str) -> ParsedRunbook | None:
        return next((r for r in self._runbooks if r.name == name), None)

    def match(
        self,
        *,
        root_signal: str | None,
        severity: str | None = None,
        service: str | None = None,
        fallback_alertname: str | None = None,
    ) -> RunbookMatch | None:
        """Matched on the root signal, never on the loudest alert (W5-11)."""
        return match(
            self._runbooks,
            root_signal=root_signal,
            severity=severity,
            service=service,
            fallback_alertname=fallback_alertname,
        )

    async def sync(self, session: AsyncSession) -> dict[str, int]:
        self._ids = await repo.sync_runbooks(session, self._runbooks)
        return self._ids


async def build_library(
    sessions: async_sessionmaker[AsyncSession], directory: Path | str
) -> RunbookLibrary:
    """Load from disk and sync to the database. Called once, at startup.

    A failure here is fatal by design: a process that starts with no runbooks
    will open war rooms with nothing pinned in them, and the only symptom is
    that responders find the bot less useful than it was yesterday.
    """
    library = RunbookLibrary.from_directory(directory)
    async with sessions() as session, session.begin():
        await library.sync(session)
    log.info("runbooks.synced", count=len(library.runbooks))
    return library
