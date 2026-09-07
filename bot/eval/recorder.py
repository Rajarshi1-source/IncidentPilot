"""Capture every external interaction, redacted at capture (W6-21, D2).

Week 7 replays incidents against fakes; this is what fills the fixtures. It sits
in week 6 because the only way to record a real interaction is to record it while
one is happening, and by week 7 the interesting ones have already gone past.

**Redaction happens at capture, not at read.** A cassette on disk containing a
customer's email is a copy of that email with none of the controls, and "we
redact when we load it" is a promise the filesystem does not keep. The redactor
runs before anything is written, and the recorded payloads are exactly what the
provider saw -- which is also what makes the D6 CI test meaningful.

Recordings are content-addressed by incident and interaction index, so a replay
that drifts out of order fails loudly rather than matching the wrong response.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(__file__).parent / "cassettes"


@dataclass
class Interaction:
    """One external call and its result."""

    index: int
    kind: str
    request: dict[str, Any]
    response: dict[str, Any]
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class Recorder:
    """Collects interactions for one incident, then writes them once.

    Written once, at the end, rather than appended per call: a half-written
    cassette is worse than none, because the replay would run happily through
    the first six interactions and then diverge in a way that looks like a code
    change rather than a truncated file.
    """

    incident_id: int
    redactor: Any | None = None
    interactions: list[Interaction] = field(default_factory=list)
    enabled: bool = True

    def record(self, kind: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.interactions.append(
            Interaction(
                index=len(self.interactions),
                kind=kind,
                request=self._clean(request),
                response=self._clean(response),
            )
        )

    def _clean(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Redact at capture. Secrets are dropped entirely, not tokenized.

        A token stands in for a value the model still needs to reason about; an
        API key is not that. Dropping the header outright means the cassette
        cannot leak a credential even if the redactor's patterns miss one.
        """
        redacted = json.loads(json.dumps(payload, default=str))
        _drop_secrets(redacted)
        if self.redactor is None:
            return redacted
        return json.loads(self.redactor.redact(json.dumps(redacted)))

    def write(self, directory: Path | None = None) -> Path:
        target = (directory or DEFAULT_DIR) / f"incident-{self.incident_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "incident_id": self.incident_id,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "interactions": [asdict(i) for i in self.interactions],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return target


SECRET_KEYS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api_key",
        "apikey",
        "token",
        "routing_key",
        "signing_secret",
        "password",
    }
)


def _drop_secrets(node: Any) -> None:
    """Walk the payload and blank anything that looks like a credential."""
    if isinstance(node, dict):
        for key in list(node):
            if key.lower() in SECRET_KEYS:
                node[key] = "<REDACTED>"
            else:
                _drop_secrets(node[key])
    elif isinstance(node, list):
        for item in node:
            _drop_secrets(item)


def load(path: Path) -> list[Interaction]:
    """Read a cassette back. Used by the week 7 replayer."""
    body = json.loads(path.read_text(encoding="utf-8"))
    return [Interaction(**i) for i in body.get("interactions", [])]
