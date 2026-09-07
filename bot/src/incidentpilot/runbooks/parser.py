"""Markdown in, structured runbook out (W5-10).

The format is deliberately plain markdown with two additions: a YAML front
matter block, and `<!-- step:some-id -->` markers before each step.

**Why HTML comments rather than headings.** A heading is prose and people
reword it -- "Verify replica lag" becomes "Check the lag" in a helpful edit, and
every efficacy number keyed on that step silently resets to zero. A marker is
obviously an identifier, invisible in every markdown renderer, and awkward to
change by accident. The id is a contract between the runbook, the detector, and
``Q5``; the heading next to it is free to be rewritten.

`git_sha` is recorded at load. When D4 eventually says "nobody runs step 3 of
the disk-full runbook", the first question is "of which version", and a runbook
table with no provenance cannot answer it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

# `<!-- step:verify-replica-lag -->` — ids are kebab-case, which the loader
# enforces rather than assumes, because `step:Verify Replica Lag` would parse
# happily here and then never match a `/step done` argument anyone could type.
STEP_MARKER = re.compile(r"^<!--\s*step:([a-z0-9][a-z0-9-]*)\s*-->\s*$", re.MULTILINE)
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

VALID_STEP_ID = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")


class RunbookParseError(ValueError):
    """The file is not a usable runbook. Raised, never silently skipped.

    A runbook that fails to load and says nothing is a runbook that is missing
    from every match, and the only symptom is that incidents stop getting one.
    """


@dataclass(frozen=True, slots=True)
class Step:
    step_id: str
    body: str

    @property
    def title(self) -> str:
        """The first heading line, for the Block Kit summary."""
        for line in self.body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
            if stripped:
                return stripped[:120]
        return self.step_id


@dataclass(frozen=True, slots=True)
class ParsedRunbook:
    name: str
    alert_pattern: str
    body: str
    steps: tuple[Step, ...]
    version: str = "1.0.0"
    severity_filter: str | None = None
    service_filter: str | None = None
    git_sha: str | None = None
    path: Path | None = None

    @property
    def step_ids(self) -> list[str]:
        return [s.step_id for s in self.steps]


def parse_runbook(
    text: str, *, path: Path | None = None, git_sha: str | None = None
) -> ParsedRunbook:
    match = FRONT_MATTER.match(text)
    if match is None:
        raise RunbookParseError(f"{path or '<text>'}: no YAML front matter")

    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise RunbookParseError(f"{path or '<text>'}: front matter is not a mapping")

    name = str(meta.get("name") or "").strip()
    alert_pattern = str(meta.get("alert_pattern") or "").strip()
    if not name:
        raise RunbookParseError(f"{path or '<text>'}: front matter has no name")
    if not alert_pattern:
        raise RunbookParseError(f"{path or '<text>'}: front matter has no alert_pattern")

    try:
        re.compile(alert_pattern)
    except re.error as exc:
        # Caught at load, not at match time. A bad pattern discovered during an
        # incident means the matcher raises at the exact moment it is needed.
        raise RunbookParseError(
            f"{path or '<text>'}: alert_pattern is not valid regex: {exc}"
        ) from exc

    body = text[match.end() :]
    steps = _split_steps(body, path)
    if not steps:
        raise RunbookParseError(f"{path or '<text>'}: no <!-- step:id --> markers")

    seen: set[str] = set()
    for step in steps:
        if not VALID_STEP_ID.match(step.step_id):
            raise RunbookParseError(
                f"{path or '<text>'}: step id {step.step_id!r} is not kebab-case"
            )
        if step.step_id in seen:
            # A duplicate id would make UNIQUE (incident_id, step_id) collapse
            # two genuinely different steps into one signal, and D4's adherence
            # number would be quietly wrong rather than obviously broken.
            raise RunbookParseError(f"{path or '<text>'}: duplicate step id {step.step_id!r}")
        seen.add(step.step_id)

    return ParsedRunbook(
        name=name,
        alert_pattern=alert_pattern,
        body=body.strip(),
        steps=tuple(steps),
        version=str(meta.get("version") or "1.0.0"),
        severity_filter=_optional(meta.get("severity_filter")),
        service_filter=_optional(meta.get("service_filter")),
        git_sha=git_sha,
        path=path,
    )


def _optional(value: object) -> str | None:
    """YAML `null` and the string "null" both mean absent."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in {"", "null", "None"} else text


def _split_steps(body: str, path: Path | None) -> list[Step]:
    markers = list(STEP_MARKER.finditer(body))
    steps: list[Step] = []
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        steps.append(Step(step_id=marker.group(1), body=body[start:end].strip()))
    return steps


def load_runbooks(directory: Path, *, git_sha: str | None = None) -> list[ParsedRunbook]:
    """Parse every ``*.md`` in a directory, in filename order.

    Filename order is the tie-break the matcher falls back on, so the numeric
    prefixes on the files are load-bearing rather than decorative.
    """
    if not directory.is_dir():
        raise RunbookParseError(f"runbook directory not found: {directory}")

    sha = git_sha if git_sha is not None else current_git_sha(directory)
    out = [
        parse_runbook(path.read_text(encoding="utf-8"), path=path, git_sha=sha)
        for path in sorted(directory.glob("*.md"))
    ]
    if not out:
        raise RunbookParseError(f"no runbooks found in {directory}")
    log.info("runbooks.loaded", count=len(out), directory=str(directory), git_sha=sha)
    return out


def current_git_sha(directory: Path) -> str | None:
    """Provenance for D4. Best-effort: a tarball deploy has no git, and that is
    a missing field rather than a failure to load runbooks."""
    try:
        # Fixed argv, no shell, no user input -- the only variable is a
        # directory path this process chose.
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None
