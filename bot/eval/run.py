"""``python -m eval.run`` — replay the corpus, score it, gate on it (W7-11).

    python -m eval.run --corpus eval/corpus                       # score and print
    python -m eval.run --corpus eval/corpus --gate                # exit 1 on failure
    python -m eval.run --corpus eval/corpus --gate --report r.md   # + sticky comment
    python -m eval.run --incident 204 --verbose                    # one incident, traced
    python -m eval.run --corpus eval/corpus --set correlation.window_s=600
    python -m eval.run --corpus eval/corpus --live                 # real providers, manual only

``--live`` exists so recordings can be refreshed when a provider changes
behaviour. **It is never what CI runs**, and it is the only mode in which a
socket may legitimately open -- so it is also the only mode that turns the guard
off, and it says so on stdout every time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from eval.gate import evaluate, load_baseline
from eval.metrics import (
    Aggregate,
    IncidentScore,
    action_item_recall,
    citation_precision,
    fabricated_citations,
    storm_compression,
    timeline_f1,
    uncited_claims,
)
from eval.overrides import OverriddenCorrelation, apply_to_models, parse_overrides, validate
from eval.recorder import Recording, corpus_paths, load_recording
from eval.replayer import Replayer, ReplayResult
from eval.report import render_markdown, render_table
from incidentpilot.config.models_config import load_models_config
from incidentpilot.telemetry.logging import configure_logging

DEFAULT_CORPUS = Path(__file__).parent / "corpus"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval.run", description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="corpus directory")
    parser.add_argument("--incident", type=int, help="replay one incident by id")
    parser.add_argument("--gate", action="store_true", help="exit 1 when the gate fails")
    parser.add_argument("--report", help="write the markdown table to this path")
    parser.add_argument("--verbose", action="store_true", help="per-incident detail")
    parser.add_argument(
        "--live",
        action="store_true",
        help="call real providers instead of replaying (manual refresh only, never CI)",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="counterfactual override, e.g. roles.synthesize.model=<id>",
    )
    parser.add_argument("--json", dest="as_json", help="write raw metrics as JSON to this path")
    return parser


def score(result: ReplayResult) -> IncidentScore:
    """Turn one replayed incident into one row of the corpus table."""
    truth = result.ground_truth
    draft = result.draft
    ctx = result.context

    if not result.expects_pir:
        # False positives and flapping alerts must produce **no PIR**. Producing
        # one is the failure, so the score is binary rather than graded: the
        # bucket exists to catch a product that writes a postmortem about an
        # alert that was never an incident.
        clean = draft is None
        return IncidentScore(
            incident_id=result.incident_id,
            bucket=result.bucket,
            timeline_f1=1.0 if clean else 0.0,
            citation_precision=1.0 if clean else 0.0,
            uncited_claims=0,
            fabricated_citations=0,
            action_item_recall=1.0 if clean else 0.0,
            storm_compression=storm_compression(
                result.correlation.incidents_created, int(truth.get("expected_incidents", 1))
            ),
            cost_usd=result.cost_usd,
            generation_ms=result.generation_ms,
            network_calls=result.network_calls,
            prompt_drift=len(result.prompt_drift),
            layer="none",
            unexpected_skeleton=0,
            error=result.error,
        )

    layer = result.pir.layer if result.pir is not None else "none"
    return IncidentScore(
        incident_id=result.incident_id,
        bucket=result.bucket,
        timeline_f1=timeline_f1(list(result.timeline), list(truth.get("timeline") or [])),
        citation_precision=citation_precision(draft, ctx),
        uncited_claims=uncited_claims(draft),
        fabricated_citations=fabricated_citations(draft, ctx),
        action_item_recall=action_item_recall(
            list(draft.action_items) if draft is not None else [],
            [str(a) for a in (truth.get("action_items") or [])],
        ),
        storm_compression=storm_compression(
            result.correlation.incidents_created, int(truth.get("expected_incidents", 1))
        ),
        cost_usd=result.cost_usd,
        generation_ms=result.generation_ms,
        network_calls=result.network_calls,
        prompt_drift=len(result.prompt_drift),
        layer=layer,
        # The recording says which fixtures are *supposed* to reach layer 3.
        # Three do -- the provider-failure bucket exists to prove the skeleton
        # ships when every vendor is gone. Everything else reaching it is a
        # regression that no other metric would report.
        unexpected_skeleton=int(layer == "skeleton" and truth.get("expected_layer") != "skeleton"),
        error=result.error,
    )


async def replay_all(recordings: list[Recording], overrides: dict[str, str]) -> list[ReplayResult]:
    config = apply_to_models(load_models_config(), overrides)
    correlation = OverriddenCorrelation(overrides)
    out: list[ReplayResult] = []
    for recording in recordings:
        replayer = Replayer(recording, config=config, correlation_cfg=correlation)
        out.append(await replayer.run())
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Quiet by default. Forty incidents at INFO is 200 lines of application log
    # in front of the table, and the table is the artifact -- a report nobody
    # scrolls to is a report nobody reads. `--verbose` restores it, which is
    # also when you actually want it.
    configure_logging(level="DEBUG" if args.verbose else "ERROR", json_output=False)
    overrides = parse_overrides(list(args.overrides))
    validate(overrides)

    if args.live:
        print(
            "--live: calling real providers. The socket guard is OFF and this run is "
            "NOT a gate. Use it to refresh recordings, never in CI.",
            file=sys.stderr,
        )
        return _live_unavailable()

    paths = corpus_paths(args.corpus)
    if not paths:
        print(f"no recordings in {args.corpus}", file=sys.stderr)
        return 1

    recordings = [load_recording(p) for p in paths]
    if args.incident is not None:
        recordings = [r for r in recordings if r.incident_id == args.incident]
        if not recordings:
            print(f"no incident {args.incident} in {args.corpus}", file=sys.stderr)
            return 1

    started = time.perf_counter()
    results = asyncio.run(replay_all(recordings, overrides))
    wall = time.perf_counter() - started

    aggregate = Aggregate()
    for result in results:
        aggregate.add(score(result))

    if args.verbose:
        for result in results:
            _trace(result)

    metrics = aggregate.as_dict()
    # Distinct, because forty fixtures drifting on one prompt is one fact.
    notes = sorted({message for result in results for message in result.prompt_drift})
    baseline = load_baseline(Path(args.corpus) / "manifest.json")
    gate = evaluate(metrics, baseline)
    network = int(metrics.get("network_calls", 0))

    print(render_table(aggregate, metrics, baseline, gate, wall_s=wall, network_calls=network))

    if args.report:
        Path(args.report).write_text(
            render_markdown(
                aggregate,
                metrics,
                baseline,
                gate,
                wall_s=wall,
                network_calls=network,
                judge_line=_judge_line(Path(args.corpus)),
                counterfactual=" ".join(f"--set {k}={v}" for k, v in overrides.items()) or None,
                notes=notes,
            ),
            encoding="utf-8",
        )

    if args.as_json:
        Path(args.as_json).write_text(
            json.dumps(
                {"metrics": metrics, "buckets": aggregate.by_bucket(), "wall_s": round(wall, 3)},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    if args.gate:
        for note in notes:
            print(f"::error::{note}")
        for failure in gate.failures:
            # GitHub renders this as an annotation on the PR, which is what
            # makes the red check readable without opening the log.
            print(f"::error::{failure}")
        return 0 if gate.passed else 1
    return 0


def _trace(result: ReplayResult) -> None:
    print(f"\n--- incident {result.incident_id} [{result.bucket}] {result.title}")
    print(
        f"    alerts {result.correlation.alerts_seen} -> incidents "
        f"{result.correlation.incidents_created}  root={result.correlation.root_signal}"
    )
    for merge in result.correlation.merges[:5]:
        print(f"    merge: {merge}")
    print(
        f"    timeline {len(result.timeline)} entries · refs "
        f"{len(result.context.valid_reference_set())} · llm calls {result.llm_calls}"
    )
    if result.pir is not None:
        print(
            f"    pir: layer={result.pir.layer} coverage={result.pir.coverage} "
            f"ms={result.pir.generation_ms}"
        )
        if result.pir.validation is not None and not result.pir.validation.ok:
            for message in result.pir.validation.messages[:5]:
                print(f"      reject: {message}")
    for drift in result.prompt_drift:
        print(f"    DRIFT {drift}")
    if result.error:
        print(f"    ERROR {result.error}")


def _judge_line(corpus: Path) -> str | None:
    from eval.judge.agreement import load_agreement

    return load_agreement(corpus)


def _live_unavailable() -> int:
    """``--live`` needs credentials this repository deliberately does not carry.

    Refusing loudly rather than falling back to replay: a `--live` run that
    silently replayed would produce a "refreshed" corpus identical to the old
    one, which is the corpus-rot failure mode with a reassuring log line
    attached.
    """
    print(
        "live mode requires provider credentials (OPENAI_API_KEY / ANTHROPIC_API_KEY) and "
        "an explicit IP_EVAL_ALLOW_LIVE=1. Nothing was replayed.",
        file=sys.stderr,
    )
    import os

    if os.environ.get("IP_EVAL_ALLOW_LIVE") != "1":
        return 2
    print(
        "IP_EVAL_ALLOW_LIVE=1 set, but live refresh is a manual procedure — see "
        "docs/ADR/0006-replay-and-the-eval-gate.md",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
