"""The harness's own guarantees (W7-02, W7-03, W7-05, W7-06, W7-11, W7-12).

A replay harness is a measuring instrument, and an uncalibrated instrument is
worse than none -- it produces numbers people act on. These tests are the
calibration: the recording round-trips, replay is deterministic, the socket
guard actually trips, ``fetch_history`` actually raises, and a ``--set`` that
names nothing is refused rather than ignored.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval.fakes.chat import ReplayChat
from eval.overrides import (
    OverriddenCorrelation,
    UnknownOverride,
    apply_to_models,
    parse_overrides,
    validate,
)
from eval.recorder import (
    FORMAT_VERSION,
    KIND_GROUND_TRUTH,
    Recorder,
    RecordingFormatError,
    corpus_paths,
    load_recording,
    parse_recording,
)
from eval.replayer import (
    NetworkCallDuringReplay,
    Replayer,
    VirtualClock,
    no_network_guard,
)
from eval.run import main, score
from incidentpilot.config.models_config import load_models_config

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "corpus"


# --- W7-02: the recording format ---------------------------------------------


def test_recording_roundtrip() -> None:
    """Write, read back, and get the same entries and labels."""
    recorder = Recorder(
        incident_id=901,
        bucket="real",
        title="a round trip",
        start=datetime(2026, 9, 1, tzinfo=UTC),
    )
    recorder.record("alert", at=datetime(2026, 9, 1, tzinfo=UTC), payload={"labels": {"a": "b"}})
    recorder.record(
        "chat.event.message",
        at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        payload={"ts": "1.0", "text": "looking"},
    )
    recorder.label(timeline=[{"t": 60, "intent": "investigation"}], action_items=["do a thing"])

    parsed = parse_recording(recorder.render())

    assert parsed.format_version == FORMAT_VERSION
    assert parsed.incident_id == 901
    assert [e.kind for e in parsed.entries] == ["alert", "chat.event.message"]
    assert parsed.entries[1].t == pytest.approx(60.0)
    assert parsed.ground_truth["action_items"] == ["do a thing"]


def test_recording_redacts_secrets_at_capture() -> None:
    """A credential must never reach the file, not even to be stripped later.

    "We redact when we load it" is a promise the filesystem does not keep: the
    corpus is committed to git, and a token in git is a token that has to be
    rotated whatever the loader does with it afterwards.
    """
    recorder = Recorder(incident_id=902, start=datetime(2026, 9, 1, tzinfo=UTC))
    recorder.record(
        "chat.conversations.create",
        at=datetime(2026, 9, 1, tzinfo=UTC),
        request={"headers": {"Authorization": "Bearer xoxb-super-secret"}, "name": "inc-1"},
    )
    rendered = recorder.render()
    assert "xoxb-super-secret" not in rendered
    assert "<REDACTED>" in rendered


def test_a_future_format_is_refused_not_guessed_at() -> None:
    """Corpus rot is the listed failure mode. Refusing is how it stays visible."""
    body = json.dumps({"format": 99, "incident": 1}) + "\n"
    with pytest.raises(RecordingFormatError, match="format 99"):
        parse_recording(body)


def test_a_malformed_line_raises_rather_than_being_skipped() -> None:
    """Scoring 39 of 40 while reporting 40 is the quiet version of a broken gate."""
    body = json.dumps({"format": 1, "incident": 1}) + "\nnot json at all\n"
    with pytest.raises(RecordingFormatError):
        parse_recording(body)


# --- W7-03: the architectural rule the fake enforces --------------------------


async def test_fetch_history_raises_on_the_replay_chat() -> None:
    """INV-02 with teeth (B-01).

    The failure this guards is silent by construction -- Slack's history limit
    produces no error, just a shorter transcript -- so an assertion is the only
    available detector.
    """
    recording = load_recording(CORPUS / "incident_201.jsonl")
    chat = ReplayChat(recording)
    with pytest.raises(AssertionError, match="event-sourced"):
        await chat.fetch_history("C123")
    with pytest.raises(AssertionError, match="event-sourced"):
        await chat.fetch_replies("C123", "1.0")


# --- W7-05 / W7-06: determinism and the socket guard --------------------------


def test_virtual_clock_advances_only_forward() -> None:
    clock = VirtualClock(start=datetime(2026, 9, 1, tzinfo=UTC))
    clock.advance_to(120.0)
    clock.advance_to(30.0)
    assert clock.now() == datetime(2026, 9, 1, 0, 2, tzinfo=UTC)


async def test_replay_is_deterministic() -> None:
    """Three runs, byte-identical scores -- every field, including latency.

    ``generation_ms`` is in the comparison rather than excluded from it, and
    that is the point. It reports the latency the *recording* captured, not how
    fast the harness replayed it; a harness that timed itself and called the
    result `p95_generation_ms` would report a 3 ms p95 against a 30 s budget --
    a number that is perfectly green and says nothing about whether the product
    meets its SLO. The first version of this file did exactly that, and this
    test is what caught it.
    """
    from dataclasses import asdict

    recording = load_recording(CORPUS / "incident_201.jsonl")
    rendered = []
    for _ in range(3):
        result = await Replayer(recording, config=load_models_config()).run()
        rendered.append(json.dumps(asdict(score(result)), sort_keys=True, default=str))
    assert len(set(rendered)) == 1, "replay is not deterministic"


def test_socket_guard_trips() -> None:
    """ "No network" is a property worth testing, not just intending."""
    with no_network_guard() as guard, pytest.raises(NetworkCallDuringReplay):
        socket.create_connection(("example.invalid", 80), timeout=0.1)
    assert guard.calls == 1
    assert "example.invalid" in guard.attempts[0]


def test_socket_guard_restores_the_real_functions() -> None:
    """A guard that leaked would break every later test in the session."""
    original = socket.socket.connect
    with no_network_guard():
        assert socket.socket.connect is not original
    assert socket.socket.connect is original


async def test_the_whole_corpus_makes_zero_network_calls() -> None:
    """INV-10 across every fixture, not just the happy one."""
    for path in corpus_paths(CORPUS):
        result = await Replayer(load_recording(path)).run()
        assert result.network_calls == 0, f"{path.name} opened a socket"
        assert result.chat_history_calls == 0, f"{path.name} called conversations.history"


# --- W7-11 / W7-12: the CLI and the counterfactual ---------------------------


def test_cli_flags(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    raw = tmp_path / "metrics.json"
    code = main(
        [
            "--corpus",
            str(CORPUS),
            "--gate",
            "--report",
            str(report),
            "--json",
            str(raw),
        ]
    )
    assert code == 0, "the committed corpus must pass its own committed baseline"
    assert "GATE: PASS" in report.read_text(encoding="utf-8")
    assert "Per bucket" in report.read_text(encoding="utf-8")
    assert json.loads(raw.read_text(encoding="utf-8"))["metrics"]["network_calls"] == 0


def test_cli_single_incident_is_scoped() -> None:
    assert main(["--corpus", str(CORPUS), "--incident", "201"]) == 0
    assert main(["--corpus", str(CORPUS), "--incident", "9999"]) == 1


def test_counterfactual_changes_outcome(tmp_path: Path) -> None:
    """Turning correlation off must visibly change the storm result.

    This is the test that proves ``--set`` is wired to something. A
    counterfactual that quietly does nothing is worse than no counterfactual:
    you run it, read the unchanged number, and conclude the parameter does not
    matter.
    """
    import asyncio

    from eval.run import replay_all

    recordings = [load_recording(CORPUS / "incident_215.jsonl")]

    normal = asyncio.run(replay_all(recordings, {}))[0]
    without = asyncio.run(replay_all(recordings, {"correlation.merge_threshold": "0.99"}))[0]

    assert normal.correlation.incidents_created == 1
    assert without.correlation.incidents_created > 30, (
        "disabling correlation must turn the 40-alert cascade into many war rooms"
    )


def test_an_unknown_override_is_refused_by_name() -> None:
    with pytest.raises(UnknownOverride, match="only roles"):
        validate(parse_overrides(["validator.support_threshold=0.8"]))
    with pytest.raises(UnknownOverride, match="not overridable"):
        apply_to_models(load_models_config(), parse_overrides(["roles.synthesize.nonsense=1"]))
    with pytest.raises(UnknownOverride, match="no such role"):
        apply_to_models(load_models_config(), parse_overrides(["roles.nope.model=x"]))


def test_role_override_reaches_the_resolved_spec() -> None:
    """E-1's mechanism, in one assertion.

    Week 7 owes the model decision a mechanism, not a verdict. This is it: the
    candidate is a flag, the committed default in ``models.yaml`` is untouched,
    and the comparison happens on this project's own corpus rather than on a
    price list.
    """
    config = apply_to_models(
        load_models_config(),
        parse_overrides(
            [
                "roles.synthesize.model=candidate-x",
                "roles.synthesize.input_usd_per_mtok=0.06",
            ]
        ),
    )
    spec = config.role("synthesize")
    assert spec.primary.model == "candidate-x"
    assert spec.primary.input_usd_per_mtok == pytest.approx(0.06)
    # The file on disk is unchanged: the counterfactual is a replay, not an edit.
    assert load_models_config().role("synthesize").primary.model != "candidate-x"


def test_correlation_override_defaults_match_the_committed_config() -> None:
    """The counterfactual's *baseline* must be the shipped configuration.

    If these drifted apart, every `--set correlation.*` run would be comparing
    against a window nobody actually deploys, and the delta it reported would be
    measured from the wrong place.
    """
    from incidentpilot.config.settings import Settings

    settings = Settings(environment="test")
    default = OverriddenCorrelation({})
    assert default.correlation_window_s == settings.correlation_window_s
    assert default.merge_threshold == pytest.approx(settings.merge_threshold)
    assert default.storm_threshold == settings.storm_threshold


# --- W7-07: the corpus itself -------------------------------------------------


def test_the_corpus_has_forty_incidents_in_eleven_buckets() -> None:
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    paths = corpus_paths(CORPUS)
    assert len(paths) == 40
    assert manifest["corpus"]["n"] == 40
    assert len(manifest["corpus"]["buckets"]) == 11
    listed = sorted(i for ids in manifest["corpus"]["buckets"].values() for i in ids)
    assert listed == sorted(load_recording(p).incident_id for p in paths)


def test_the_adversarial_bucket_actually_plants_a_reference() -> None:
    """A fixture that does not contain the bait tests nothing.

    The planted ID is well-formed under the evidence grammar and absent from
    the store, which is precisely the shape a model is most likely to copy and
    the validator most needs to reject.
    """
    from incidentpilot.domain.evidence import is_reference

    planted = "msg:1757000000.000100"
    assert is_reference(planted)

    texts: list[str] = []
    for incident_id in (239, 240):
        recording = load_recording(CORPUS / f"incident_{incident_id}.jsonl")
        texts.extend(e.payload.get("text", "") for e in recording.entries)
        assert planted not in recording.ground_truth.get("valid_refs", [])
    assert any(planted in text for text in texts)


def test_the_buckets_that_must_produce_no_pir_are_labelled() -> None:
    for path in corpus_paths(CORPUS):
        recording = load_recording(path)
        if recording.bucket in {"flapping", "abandoned"}:
            assert not recording.expects_pir, f"{path.name} must produce no PIR"
        else:
            assert recording.expects_pir, f"{path.name} should produce a PIR"


def test_every_recording_ends_with_ground_truth() -> None:
    """Labels live inside the recording, never in a sidecar spreadsheet.

    Two files drift; one does not. A label that no longer matches the incident
    it scores permanently distorts every future comparison, and nothing in CI
    would notice.
    """
    for path in corpus_paths(CORPUS):
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert json.loads(lines[-1])["kind"] == KIND_GROUND_TRUTH, path.name
        assert json.loads(lines[0])["format"] == FORMAT_VERSION, path.name


def test_no_recording_carries_a_credential() -> None:
    """D6 applied to the corpus. It is committed to git; git does not forget."""
    import re

    leaky = re.compile(r"xoxb-[A-Za-z0-9-]+|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
    for path in corpus_paths(CORPUS):
        body = path.read_text(encoding="utf-8")
        assert not leaky.search(body), f"{path.name} contains something shaped like a credential"
