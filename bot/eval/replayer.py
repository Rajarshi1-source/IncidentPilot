"""Deterministic replay against fakes (W7-05, W7-06, INV-10).

**Determinism comes from the architecture, not from a seed.** ``domain/`` is
pure, every boundary is an adapter, and time is injected -- so the same recording
produces byte-identical results on every run without anything here having to
seed a random number generator. If replay is ever flaky, the bug is an
unadaptered clock, a ``random`` call, or a network call, and the fix is to
adapter it rather than to seed it.

**No database.** The pipeline runs entirely in memory: correlation over pure
domain functions, transcript over ``detect_intent``, and a ``GroundedContext``
assembled from the recorded entries rather than queried. That is not a shortcut
around the persistence layer -- it is the only way to honour INV-10, because a
Postgres connection *is* a socket and the guard below would trip on it. What the
replay measures is the reasoning, and the reasoning is the part a prompt or
model change can break.

**The socket guard is an assertion, not a hope.** "No network" is a property
worth testing. It runs for the duration of every replay, it counts, and any
connection attempt fails the run with the address it tried to reach.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from eval.fakes.chat import ReplayChat
from eval.fakes.llm import ReplayLLM
from eval.fakes.metrics import ReplayMetrics
from eval.fakes.paging import ReplayPaging
from eval.recorder import (
    KIND_ALERT,
    KIND_DEPLOY,
    KIND_MESSAGE,
    KIND_MESSAGE_CHANGED,
    KIND_RUNBOOK_STEP,
    Recording,
)
from incidentpilot.adapters.llm.router import LLMRouter
from incidentpilot.config.graph_loader import load_service_graph
from incidentpilot.config.models_config import ModelsConfig, load_models_config
from incidentpilot.domain.correlation import (
    OpenIncident,
    correlate,
    pick_root_signal,
)
from incidentpilot.domain.intent import IntentKind, detect_intent
from incidentpilot.domain.normalize import NormalizedAlert, normalize_alertmanager
from incidentpilot.pir.context import (
    AlertRef,
    DeployRef,
    GroundedContext,
    MessageRef,
    RunbookStepRef,
    TimelineRef,
)
from incidentpilot.pir.generator import PIRGenerator, PIRResult
from incidentpilot.pir.prompts.registry import PromptIntegrityError, get_prompt


class NetworkCallDuringReplay(AssertionError):
    """A socket was opened while replaying. The run is void."""


def current_prompt(role: str = "synthesize") -> tuple[str, str | None]:
    """``(sha256, integrity_error)`` for the prompt behind the corpus.

    Resolved once per replay rather than lazily inside the generator, because
    the registry raises ``PromptIntegrityError`` when a prompt file no longer
    matches its recorded hash -- and an exception thrown forty times reports
    "replay_errors: 40", which is true and useless. The reviewer needs to read
    "the prompt changed and the corpus is now stale", and they need to read it
    on the row that says so.
    """
    try:
        return get_prompt(role).sha256, None
    except PromptIntegrityError as exc:
        return "", str(exc).splitlines()[0]


@dataclass
class VirtualClock:
    """Time as an injected value, advanced by the recording (W7-05).

    Every window, every timeout and every "how long did this take" in the system
    reads a clock that is a constructor argument. That is what makes a fixture
    recorded in September score identically in March -- and it is the single
    design decision the whole harness rests on.
    """

    start: datetime
    offset: float = 0.0

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.offset)

    def advance_to(self, t: float) -> None:
        """Move forward to a recorded offset. Never backwards.

        Recordings are sorted by ``t`` before replay, so a backwards step means
        a fixture was hand-edited into an inconsistent order -- clamping it
        silently would let that fixture score, which is how a corpus rots.
        """
        self.offset = max(self.offset, t)


@dataclass
class SocketGuard:
    """Counts and refuses connection attempts (W7-06, INV-10)."""

    attempts: list[str] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.attempts)


@contextmanager
def no_network_guard(guard: SocketGuard | None = None) -> Iterator[SocketGuard]:
    """Fail the run on any outbound connection.

    Patches ``connect`` rather than ``socket()`` construction: creating a socket
    object is harmless and several libraries do it at import time, while
    *connecting* is the thing INV-10 forbids. Guarding the wrong verb would
    either miss real calls or fail on imports, and both would end with someone
    switching the guard off.
    """
    tracker = guard or SocketGuard()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def _refuse(address: Any) -> None:
        tracker.attempts.append(str(address))
        raise NetworkCallDuringReplay(
            f"replay attempted a network call to {address!r}. The corpus must run "
            "offline (INV-10) -- an adapter is reaching for a real dependency."
        )

    def _connect(self: Any, address: Any) -> None:
        _refuse(address)

    def _connect_ex(self: Any, address: Any) -> int:
        _refuse(address)
        return 1  # pragma: no cover - _refuse always raises

    def _create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        _refuse(address)
        raise AssertionError  # pragma: no cover - _refuse always raises

    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
    socket.create_connection = _create_connection
    try:
        yield tracker
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.create_connection = real_create


@dataclass(frozen=True, slots=True)
class ReplayCorrelation:
    """What correlation did, for the storm-compression metric and the report."""

    incidents_created: int
    alerts_seen: int
    root_signal: str | None
    merges: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PredictedTimelineEntry:
    """A timeline row as the system would have produced it."""

    t: float
    intent: str
    description: str


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Everything one replayed incident yields to the scorers."""

    incident_id: int
    bucket: str
    title: str
    correlation: ReplayCorrelation
    timeline: tuple[PredictedTimelineEntry, ...]
    context: GroundedContext
    pir: PIRResult | None
    ground_truth: dict[str, Any]
    expects_pir: bool
    cost_usd: float
    generation_ms: int
    network_calls: int
    chat_history_calls: int
    prompt_drift: tuple[str, ...]
    llm_calls: int
    error: str | None = None

    @property
    def draft(self) -> Any:
        return self.pir.draft if self.pir is not None else None


class Replayer:
    """One recording in, one ``ReplayResult`` out."""

    def __init__(
        self,
        recording: Recording,
        *,
        config: ModelsConfig | None = None,
        correlation_cfg: Any = None,
    ) -> None:
        self.recording = recording
        self.clock = VirtualClock(start=recording.start)
        self.config = config or load_models_config()
        self.cfg = correlation_cfg or _DefaultCorrelationConfig()
        self.chat = ReplayChat(recording)
        self.paging = ReplayPaging(recording)
        self.metrics = ReplayMetrics(recording)
        self.llm = ReplayLLM(recording)
        self._prompt_sha = ""
        self._integrity_error: str | None = None

    async def run(self) -> ReplayResult:
        with no_network_guard() as guard:
            return await self._run(guard)

    async def _run(self, guard: SocketGuard) -> ReplayResult:
        rec = self.recording
        graph = load_service_graph()
        self._prompt_sha, self._integrity_error = current_prompt()

        open_incidents: list[OpenIncident] = []
        alerts: list[NormalizedAlert] = []
        merges: list[str] = []
        messages: list[MessageRef] = []
        timeline: list[PredictedTimelineEntry] = []
        deploys: list[DeployRef] = []
        steps: list[RunbookStepRef] = []
        next_incident_id = 1
        by_dedup_key: dict[str, int] = {}

        for entry in rec.entries:
            self.clock.advance_to(entry.t)

            if entry.kind == KIND_ALERT:
                alert = normalize_alertmanager(entry.payload, entry.body.get("group") or {})
                alerts.append(alert)

                # Dedup key before correlation, exactly as the write path does.
                # An alert whose dedup_key already belongs to an open incident
                # *reopens* it -- that is `UNIQUE (dedup_key, dedup_epoch)`
                # doing its job, and it is a different mechanism from
                # correlation entirely. Skipping it here would score the
                # reopened bucket as two incidents and quietly report the
                # state machine's most-used branch as a storm-compression
                # regression, which is a metric blaming the wrong subsystem.
                reopened = by_dedup_key.get(alert.dedup_key)
                if reopened is not None:
                    open_incidents = [
                        inc.absorb(alert) if inc.id == reopened else inc for inc in open_incidents
                    ]
                    continue

                decision = correlate(alert, open_incidents, graph, self.cfg)
                if decision.is_merge:
                    merges.append(decision.explanation)
                    by_dedup_key.setdefault(alert.dedup_key, int(decision.merge_into or 0))
                    open_incidents = [
                        inc.absorb(alert) if inc.id == decision.merge_into else inc
                        for inc in open_incidents
                    ]
                else:
                    open_incidents.append(
                        OpenIncident(
                            id=next_incident_id,
                            severity_rank=alert.severity_rank,
                            detected_at=alert.starts_at,
                            primary_service=alert.service,
                            stable_labels=dict(alert.stable_labels),
                            affected_services=frozenset(
                                {alert.service} if alert.service else set()
                            ),
                            last_alert_at=alert.starts_at,
                        )
                    )
                    by_dedup_key[alert.dedup_key] = next_incident_id
                    next_incident_id += 1

            elif entry.kind in (KIND_MESSAGE, KIND_MESSAGE_CHANGED):
                payload = entry.payload
                text = str(payload.get("text") or "")
                ts = str(payload.get("ts") or f"{int(rec.at(entry.t).timestamp())}.000100")
                user = payload.get("user")
                if entry.kind == KIND_MESSAGE_CHANGED:
                    # Revisions are appended, never applied in place (W4). The
                    # latest text is what the PIR reads; the original stays in
                    # the transcript, which is why a superseded message is still
                    # a citable reference.
                    messages = [
                        MessageRef(ts=m.ts, user_id=m.user_id, text=text) if m.ts == ts else m
                        for m in messages
                    ]
                else:
                    messages.append(MessageRef(ts=ts, user_id=user, text=text))

                intent = detect_intent(text)
                if intent.kind is not IntentKind.NOISE:
                    timeline.append(
                        PredictedTimelineEntry(
                            t=entry.t, intent=str(intent.kind), description=intent.summary
                        )
                    )

            elif entry.kind == KIND_DEPLOY:
                payload = entry.payload
                deploys.append(
                    DeployRef(
                        sha=str(payload.get("sha") or ""),
                        service=str(payload.get("service") or ""),
                        deployed_at=rec.at(entry.t),
                        title=payload.get("title"),
                        actor=payload.get("actor"),
                    )
                )

            elif entry.kind == KIND_RUNBOOK_STEP:
                payload = entry.payload
                steps.append(
                    RunbookStepRef(
                        runbook_id=int(payload.get("runbook_id") or 0),
                        step_id=str(payload.get("step_id") or ""),
                        detected_by=str(payload.get("detected_by") or "message"),
                    )
                )

        root = pick_root_signal(alerts, graph)
        ctx = self._context(rec, alerts, root, messages, timeline, deploys, steps)

        pir: PIRResult | None = None
        error: str | None = None
        if rec.expects_pir and self._integrity_error is None:
            try:
                pir = await self._generate(ctx)
            except Exception as exc:
                # One fixture must not abort the corpus. A replay that raises is
                # a scored failure with its traceback in the report; a harness
                # that stops on the first exception reports 12 incidents and
                # calls it a pass.
                error = f"{type(exc).__name__}: {exc}"

        return ReplayResult(
            incident_id=rec.incident_id,
            bucket=rec.bucket,
            title=rec.title,
            correlation=ReplayCorrelation(
                incidents_created=len(open_incidents),
                alerts_seen=len(alerts),
                root_signal=root.fingerprint if root is not None else None,
                merges=tuple(merges),
            ),
            timeline=tuple(timeline),
            context=ctx,
            pir=pir,
            ground_truth=dict(rec.ground_truth),
            expects_pir=rec.expects_pir,
            cost_usd=self.llm.total_cost_usd,
            # The recorded production latency, not the replay's own. See
            # ReplayLLM.total_latency_ms -- the budget row is about whether the
            # product meets its 30 s SLO, and replay speed says nothing about
            # that.
            generation_ms=self.llm.total_latency_ms,
            network_calls=guard.calls,
            chat_history_calls=self.chat.call_count("conversations.history"),
            prompt_drift=self._drift(),
            llm_calls=self.llm.calls,
            error=error,
        )

    # -- context ---------------------------------------------------------

    def _context(
        self,
        rec: Recording,
        alerts: list[NormalizedAlert],
        root: NormalizedAlert | None,
        messages: list[MessageRef],
        timeline: list[PredictedTimelineEntry],
        deploys: list[DeployRef],
        steps: list[RunbookStepRef],
    ) -> GroundedContext:
        """Assemble the evidence the way ``build_grounded_context`` would.

        The same objects, built from the recording instead of from six queries.
        Reference IDs come from ``domain/evidence.py`` here as everywhere else --
        building them by hand in the harness would mean the harness validated a
        grammar the product does not use.
        """
        labels = rec.ground_truth
        detected = alerts[0].starts_at if alerts else rec.start
        resolved_offset = labels.get("resolved_t")
        resolved = (
            rec.at(float(resolved_offset))
            if resolved_offset is not None
            else (rec.at(rec.entries[-1].t) if rec.entries else None)
        )

        ctx = GroundedContext(
            incident_id=rec.incident_id,
            public_key=f"INC-{rec.incident_id:04d}",
            title=rec.title or "replayed incident",
            severity=str(labels.get("severity") or (alerts[0].severity if alerts else "sev3")),
            detected_at=detected,
            resolved_at=resolved,
            service=str(labels.get("service") or "") or (alerts[0].service if alerts else None),
            runbook_name=labels.get("runbook"),
        )
        ctx.messages = list(messages)
        ctx.timeline = [
            TimelineRef(
                id=f"{int(rec.at(e.t).timestamp())}-{e.intent}",
                at=rec.at(e.t),
                intent=e.intent,
                description=e.description,
                author_user_id=None,
            )
            for e in timeline
        ]
        ctx.alerts = [
            AlertRef(
                fingerprint=a.fingerprint,
                alertname=a.alertname,
                service=a.service,
                starts_at=a.starts_at,
                is_root_signal=root is not None and a.fingerprint == root.fingerprint,
            )
            for a in alerts
        ]
        ctx.deploys = list(deploys)
        ctx.runbook_steps = list(steps)
        return ctx

    # -- generation ------------------------------------------------------

    async def _generate(self, ctx: GroundedContext) -> PIRResult:
        spec = self.config.role("synthesize")
        self.llm.set_rates(
            input_usd_per_mtok=spec.primary.input_usd_per_mtok,
            output_usd_per_mtok=spec.primary.output_usd_per_mtok,
        )
        # Every rung of the chain resolves to the same replay provider. The
        # recording already contains which attempt succeeded and which failed,
        # so the *chain* is what is under test rather than the individual
        # vendors -- wiring a second fake here would let a fallback fixture pass
        # by taking a path production never took.
        names = {spec.primary.provider, "fake", "replay"}
        if spec.secondary is not None:
            names.add(spec.secondary.provider)
        providers = dict.fromkeys(names, self.llm)
        router = LLMRouter(self.config, providers, offline=True)
        generator = PIRGenerator(
            router,
            metrics=self.metrics,
            now=self.clock.now,
            context_loader=lambda _session, _id: ctx,
        )
        return await generator.generate(None, ctx.incident_id)

    def _drift(self) -> tuple[str, ...]:
        """Prompts whose content no longer matches the recording (B-11).

        This is the mechanism behind G7. A one-word edit to ``pir_v2_1_0.md``
        changes its sha256, the recorded responses were produced by the previous
        text, and the gate says so by name rather than letting a stale corpus
        score as if nothing happened.

        Two paths reach here, and both are drift. The registry catches an edit
        that did not update ``registry.yaml`` -- the common case, and the one
        G7 demonstrates. The hash comparison catches an edit that *did* update
        the registry, where the file and its entry agree with each other and
        neither agrees with the corpus. The second is the more dangerous of the
        two precisely because everything looks consistent.
        """
        if self._integrity_error is not None:
            return (
                f"synthesize prompt no longer matches its registry entry: "
                f"{self._integrity_error} — the corpus was recorded against the "
                "previous text, so it can no longer score this one. Re-record with "
                "--live, or revert.",
            )
        if not self.llm.recorded_prompt_hashes:
            return ()
        current = self._prompt_sha
        drifted = {h for h in self.llm.recorded_prompt_hashes if h and h != current}
        if not drifted:
            return ()
        return tuple(
            f"synthesize: corpus recorded {h[:12]}, working tree has {current[:12]} — "
            "the recorded responses were produced by a different prompt"
            for h in sorted(drifted)
        )


class _DefaultCorrelationConfig:
    """The committed defaults, as a plain object.

    Correlation takes its config as a parameter (INV-01), which is exactly what
    makes ``--set correlation.window_s=600`` a flag instead of a patch.
    """

    def __init__(
        self,
        correlation_window_s: int = 300,
        merge_threshold: float = 0.62,
        storm_threshold: int = 5,
    ) -> None:
        self._window = correlation_window_s
        self._threshold = merge_threshold
        self._storm = storm_threshold

    @property
    def correlation_window_s(self) -> int:
        return self._window

    @property
    def merge_threshold(self) -> float:
        return self._threshold

    @property
    def storm_threshold(self) -> int:
        return self._storm


def utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
