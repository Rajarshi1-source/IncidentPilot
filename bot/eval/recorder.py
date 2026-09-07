"""The recording format, and the production-side capture that fills it (W7-02).

One JSONL file per incident. A ``{"format": 1}`` header line, then one line per
external interaction, each carrying ``t`` -- seconds from the incident's start --
and a ``kind``. The last line is ``ground_truth``: the human labels every metric
is scored against.

**Why the version header exists.** The listed failure mode of an eval harness is
not that it breaks; it is that the corpus rots. Field names change, a recording
made in week 7 stops meaning what it meant, and the gate quietly starts scoring
something else. A format number at the top of every file makes corpus migration
a normal task with a normal diff rather than a crisis nobody notices.

**Why ``t`` is relative.** Absolute timestamps would make every fixture expire:
a window computed against ``now`` behaves differently in January than it did in
September, and a corpus whose results drift with the calendar is not a baseline.
The replayer's virtual clock reconstitutes absolute time from a fixed start.

**Redaction happens at capture, not at read.** A cassette on disk containing a
customer's email is a copy of that email with none of the controls, and "we
redact when we load it" is a promise the filesystem does not keep. Secrets are
*dropped* rather than tokenized -- a token stands in for a value the model still
has to reason about, and an API key is not that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1

CORPUS_DIR = Path(__file__).parent / "corpus"

# --- the kinds ----------------------------------------------------------------
# Named as constants because three modules match on them and a typo in a string
# literal is a silently-skipped entry rather than an error.
KIND_ALERT = "alert"
KIND_MESSAGE = "chat.event.message"
KIND_MESSAGE_CHANGED = "chat.event.message_changed"
KIND_CHANNEL_CREATE = "chat.conversations.create"
KIND_ONCALL = "paging.oncall"
KIND_PAGE = "paging.page"
KIND_PROMQL = "promql"
KIND_LLM = "llm"
KIND_DEPLOY = "deploy"
KIND_RUNBOOK_STEP = "runbook.step"
KIND_STATE = "state"
KIND_GROUND_TRUTH = "ground_truth"


class RecordingFormatError(ValueError):
    """The file is not a recording this harness understands."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One line of a recording."""

    t: float
    kind: str
    body: dict[str, Any]

    @property
    def payload(self) -> dict[str, Any]:
        return dict(self.body.get("payload") or {})

    @property
    def request(self) -> dict[str, Any]:
        return dict(self.body.get("request") or {})

    @property
    def response(self) -> dict[str, Any]:
        return dict(self.body.get("response") or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)


@dataclass(frozen=True, slots=True)
class Recording:
    """A parsed incident recording: the header, the entries, and the labels."""

    incident_id: int
    bucket: str
    title: str
    start: datetime
    entries: tuple[Entry, ...]
    ground_truth: dict[str, Any]
    path: Path | None = None
    format_version: int = FORMAT_VERSION

    def of_kind(self, *kinds: str) -> list[Entry]:
        wanted = set(kinds)
        return [e for e in self.entries if e.kind in wanted]

    def at(self, offset: float) -> datetime:
        """Absolute time for a recorded offset. The virtual clock's arithmetic."""
        return self.start + timedelta(seconds=offset)

    @property
    def expects_pir(self) -> bool:
        """Whether a PIR should be produced at all.

        False positives and flapping alerts must produce **no PIR**, and that is
        a labelled expectation rather than an inference from the entries -- a
        harness that decided this for itself could not catch the bug where the
        product writes a postmortem about an alert that was never an incident.
        """
        return bool(self.ground_truth.get("expects_pir", True))

    @property
    def expected_incidents(self) -> int:
        return int(self.ground_truth.get("expected_incidents", 1))

    @property
    def expected_alerts(self) -> int:
        return int(self.ground_truth.get("expected_alerts", len(self.of_kind(KIND_ALERT))))


# --- reading ------------------------------------------------------------------


def parse_recording(text: str, *, path: Path | None = None) -> Recording:
    """Parse one recording. Strict on purpose.

    A malformed line raises rather than being skipped. A harness that tolerates
    a broken fixture scores 39 incidents while reporting 40, and the number it
    reports is the number that goes in the README.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise RecordingFormatError(f"{path or '<memory>'}: empty recording")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RecordingFormatError(f"{path or '<memory>'}: header is not JSON: {exc}") from exc

    version = int(header.get("format", 0))
    if version != FORMAT_VERSION:
        raise RecordingFormatError(
            f"{path or '<memory>'}: recording format {version}, this harness reads "
            f"{FORMAT_VERSION}. Migrate the corpus rather than loosening this check -- "
            "a fixture read under the wrong format scores something other than what it says."
        )

    start = _parse_dt(header.get("start")) or datetime(2026, 9, 1, tzinfo=UTC)

    entries: list[Entry] = []
    ground_truth: dict[str, Any] = {}
    for number, line in enumerate(lines[1:], start=2):
        try:
            body = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecordingFormatError(f"{path or '<memory>'}:{number}: {exc}") from exc
        kind = str(body.get("kind") or "")
        if not kind:
            raise RecordingFormatError(f"{path or '<memory>'}:{number}: entry has no kind")
        if kind == KIND_GROUND_TRUTH:
            ground_truth = dict(body.get("labels") or {})
            continue
        entries.append(Entry(t=float(body.get("t", 0.0)), kind=kind, body=body))

    # Sorted by t, stably. Recordings are appended in wall-clock order in
    # production, but a hand-authored fixture is written in the order a person
    # thinks about the incident, and replay ordering must not depend on that.
    entries.sort(key=lambda e: e.t)

    return Recording(
        incident_id=int(header.get("incident", 0)),
        bucket=str(header.get("bucket") or "unspecified"),
        title=str(header.get("title") or ""),
        start=start,
        entries=tuple(entries),
        ground_truth=ground_truth,
        path=path,
        format_version=version,
    )


def load_recording(path: Path) -> Recording:
    return parse_recording(path.read_text(encoding="utf-8"), path=path)


def corpus_paths(directory: Path | str) -> list[Path]:
    """Every recording in a corpus directory, in a stable order.

    Sorted by name rather than by directory order: two machines enumerating a
    directory differently would produce two different p95 latencies from the
    same corpus, and "deterministic except for the ordering" is not
    deterministic.
    """
    return sorted(Path(directory).glob("incident_*.jsonl"))


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- writing ------------------------------------------------------------------


@dataclass
class Recorder:
    """Collects an incident's interactions, then writes them once.

    Written once, at the end, rather than appended per call: a half-written
    recording is worse than none, because replay runs happily through the first
    six interactions and then diverges in a way that looks like a code change
    rather than a truncated file.
    """

    incident_id: int
    bucket: str = "real"
    title: str = ""
    start: datetime = field(default_factory=lambda: datetime.now(UTC))
    redactor: Any | None = None
    enabled: bool = True
    lines: list[dict[str, Any]] = field(default_factory=list)
    ground_truth: dict[str, Any] = field(default_factory=dict)

    def record(self, kind: str, *, at: datetime | None = None, **body: Any) -> None:
        """Append one interaction, redacted before it is held in memory at all."""
        if not self.enabled:
            return
        moment = at or datetime.now(UTC)
        entry: dict[str, Any] = {
            "t": round((moment - self.start).total_seconds(), 3),
            "kind": kind,
        }
        entry.update({key: self._clean(value) for key, value in body.items()})
        self.lines.append(entry)

    def label(self, **labels: Any) -> None:
        """Attach ground truth. Human-authored, once, carefully."""
        self.ground_truth.update(labels)

    def _clean(self, value: Any) -> Any:
        cleaned = json.loads(json.dumps(value, default=str))
        _drop_secrets(cleaned)
        if self.redactor is None:
            return cleaned
        return json.loads(self.redactor.redact(json.dumps(cleaned)))

    def render(self) -> str:
        header = {
            "format": FORMAT_VERSION,
            "incident": self.incident_id,
            "bucket": self.bucket,
            "title": self.title,
            "start": self.start.astimezone(UTC).isoformat(),
        }
        out = [json.dumps(header, sort_keys=True)]
        out.extend(json.dumps(line, sort_keys=True) for line in self.lines)
        out.append(
            json.dumps(
                {"t": self._last_t(), "kind": KIND_GROUND_TRUTH, "labels": self.ground_truth}
            )
        )
        return "\n".join(out) + "\n"

    def _last_t(self) -> float:
        return round(max((float(line["t"]) for line in self.lines), default=0.0) + 0.1, 3)

    def write(self, directory: Path | None = None) -> Path:
        target = (directory or CORPUS_DIR) / f"incident_{self.incident_id:03d}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render(), encoding="utf-8")
        return target


SECRET_KEYS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api_key",
        "apikey",
        "token",
        "bot_token",
        "routing_key",
        "signing_secret",
        "password",
        "secret",
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
