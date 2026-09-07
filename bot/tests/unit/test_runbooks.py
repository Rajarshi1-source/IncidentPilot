"""W5-09..W5-13: parse, match, render, detect (D4).

The matcher test is the one that carries the differentiator. Everything else
here is careful plumbing; ``test_runbook_matches_root_not_loudest`` is the
decision an interviewer would ask about.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from incidentpilot.runbooks import detector, renderer
from incidentpilot.runbooks.matcher import match
from incidentpilot.runbooks.parser import (
    ParsedRunbook,
    RunbookParseError,
    load_runbooks,
    parse_runbook,
)

RUNBOOKS_DIR = Path(__file__).resolve().parents[3] / "runbooks"

MINIMAL = """---
name: Test Runbook
alert_pattern: "TestAlert"
version: 2.0.0
---

Intro.

<!-- step:first-step -->
### Do the first thing

```bash
psql -h {{primary_host}} -c "SELECT pg_is_in_recovery();"
```

<!-- step:second-step -->
### Do the second thing
"""


@pytest.fixture(scope="module")
def catalogue() -> list[ParsedRunbook]:
    return load_runbooks(RUNBOOKS_DIR)


# --- the catalogue (W5-09) ----------------------------------------------------


def test_all_runbooks_parse(catalogue: list[ParsedRunbook]) -> None:
    """Appendix C asks for eight. Parsing is what makes them real."""
    assert len(catalogue) == 8
    assert all(r.steps for r in catalogue)


def test_every_runbook_has_five_steps(catalogue: list[ParsedRunbook]) -> None:
    """Appendix C specifies five step ids per runbook.

    Not arbitrary: a runbook long enough to need scrolling during an incident is
    a runbook nobody finishes, and D4's adherence number becomes meaningless when
    the denominator is twenty.
    """
    for runbook in catalogue:
        assert len(runbook.steps) == 5, f"{runbook.name} has {len(runbook.steps)} steps"


def test_step_ids_extracted(catalogue: list[ParsedRunbook]) -> None:
    """W5-10. These ids are a contract with ``Q5`` and with `/step done`."""
    replica = next(r for r in catalogue if "Replica" in r.name)
    assert replica.step_ids == [
        "verify-replica-lag",
        "check-wal-backlog",
        "check-primary-load",
        "promote-replica",
        "verify-recovery",
    ]


def test_step_ids_are_unique_within_a_runbook(catalogue: list[ParsedRunbook]) -> None:
    """A duplicate would make UNIQUE (incident_id, step_id) merge two different
    steps into one signal, and adherence would be quietly wrong."""
    for runbook in catalogue:
        assert len(set(runbook.step_ids)) == len(runbook.step_ids)


def test_every_alert_pattern_compiles(catalogue: list[ParsedRunbook]) -> None:
    """Checked at load rather than at match time.

    A bad pattern discovered while matching means the matcher raises at the
    exact moment an incident needs it.
    """
    import re

    for runbook in catalogue:
        re.compile(runbook.alert_pattern)


def test_the_demo_runbook_is_the_richest(catalogue: list[ParsedRunbook]) -> None:
    """Appendix C: runbook 7 is the one in screenshots 2 and 4, so it is built
    first and best."""
    replica = next(r for r in catalogue if "Replica" in r.name)
    assert len(replica.body) > max(len(r.body) for r in catalogue if "Replica" not in r.name), (
        "the demo runbook should be the most developed of the eight"
    )


# --- the parser ---------------------------------------------------------------


def test_parses_front_matter_and_steps() -> None:
    runbook = parse_runbook(MINIMAL)
    assert runbook.name == "Test Runbook"
    assert runbook.version == "2.0.0"
    assert runbook.severity_filter is None
    assert runbook.step_ids == ["first-step", "second-step"]


@pytest.mark.parametrize(
    ("text", "because"),
    [
        ("no front matter here", "missing front matter"),
        ("---\nname: x\n---\n\n<!-- step:a -->\nbody\n", "missing alert_pattern"),
        ('---\nname: x\nalert_pattern: "A"\n---\n\nno markers', "no steps"),
        ('---\nname: x\nalert_pattern: "[unclosed"\n---\n\n<!-- step:a -->\nb\n', "bad regex"),
        (
            '---\nname: x\nalert_pattern: "A"\n---\n\n<!-- step:a -->\nb\n<!-- step:a -->\nc\n',
            "duplicate step id",
        ),
    ],
)
def test_a_broken_runbook_raises_rather_than_being_skipped(text: str, because: str) -> None:
    """Silently skipping a bad file means it is missing from every match, and
    the only symptom is that incidents stop getting a runbook."""
    with pytest.raises(RunbookParseError):
        parse_runbook(text)


# --- the matcher (W5-11) ------------------------------------------------------


def test_runbook_matches_root_not_loudest(catalogue: list[ParsedRunbook]) -> None:
    """The differentiator.

    A storm: one `PostgresReplicationLag` (the root signal, on the deepest
    dependency) and thirty-nine `HighErrorRate` alerts it caused. Matching on the
    loudest alert pins the error-rate runbook to a database incident and sends
    the responder through the wrong list while the cause sits one hop away.
    """
    matched = match(
        catalogue,
        root_signal="PostgresReplicationLag",
        severity="sev2",
        fallback_alertname="HighErrorRate",
    )
    assert matched is not None
    assert "Replica" in matched.runbook.name
    assert matched.matched_on == "PostgresReplicationLag"


def test_the_fallback_is_only_used_when_the_root_signal_matches_nothing(
    catalogue: list[ParsedRunbook],
) -> None:
    """A single uncorrelated alert has no root signal; that is what this is for.

    It is not a second chance for the loudest alert to win.
    """
    matched = match(
        catalogue,
        root_signal="SomethingNobodyWroteARunbookFor",
        severity="sev2",
        fallback_alertname="DiskFull",
    )
    assert matched is not None
    assert matched.runbook.name == "Disk Full"
    assert matched.matched_on == "DiskFull"


def test_severity_filter_narrows_the_match(catalogue: list[ParsedRunbook]) -> None:
    """`ServiceDown` is filtered to sev1. A sev3 ServiceDown gets nothing rather
    than a runbook written for a different urgency."""
    assert match(catalogue, root_signal="ServiceDown", severity="sev1") is not None
    assert match(catalogue, root_signal="ServiceDown", severity="sev3") is None


def test_no_match_is_a_legitimate_answer(catalogue: list[ParsedRunbook]) -> None:
    """A war room with no runbook is worse than the right one and far better
    than the wrong one, because a wrong runbook gets followed."""
    assert match(catalogue, root_signal="EntirelyNovelAlert") is None


def test_a_more_specific_runbook_wins(catalogue: list[ParsedRunbook]) -> None:
    """Specificity is counted, not ranked by file order, so adding a narrower
    runbook does not require reordering the ones already there."""
    generic = parse_runbook(
        '---\nname: Generic\nalert_pattern: "DiskFull"\n---\n\n<!-- step:a -->\nb\n'
    )
    specific = parse_runbook(
        '---\nname: Specific\nalert_pattern: "DiskFull"\nseverity_filter: sev2\n'
        'service_filter: "payments"\n---\n\n<!-- step:a -->\nb\n'
    )
    matched = match(
        [generic, specific], root_signal="DiskFull", severity="sev2", service="payments"
    )
    assert matched is not None and matched.runbook.name == "Specific"


# --- the renderer (W5-12) -----------------------------------------------------


def test_render_substitutes() -> None:
    context = renderer.render_context(
        service="payments", alertname="ReplicaLag", severity="sev2", primary_host="db-1"
    )
    rendered = renderer.substitute("psql -h {{primary_host}} for {{service}}", context)
    assert rendered == "psql -h db-1 for payments"


def test_an_unknown_placeholder_stays_visible() -> None:
    """`psql -h  -c "..."` looks runnable, fails confusingly, and costs a minute
    of an incident. The marker left in place is obvious to a human."""
    context = renderer.render_context(service="payments", alertname="A", severity="sev2")
    rendered = renderer.substitute("psql -h {{primary_host}}", context)
    assert rendered == "psql -h {{primary_host}}"
    assert renderer.unresolved(rendered) == ["primary_host"]


def test_namespace_defaults_rather_than_dangling() -> None:
    """Almost every deployment uses one namespace, and `kubectl -n default` at
    least runs."""
    context = renderer.render_context(service="p", alertname="A", severity="sev2")
    assert renderer.substitute("kubectl -n {{namespace}}", context) == "kubectl -n default"


def test_blocks_carry_the_step_id_for_every_step(catalogue: list[ParsedRunbook]) -> None:
    """`/step done verify-replica-lag` requires the responder to read the id off
    the message."""
    replica = next(r for r in catalogue if "Replica" in r.name)
    context = renderer.render_context(service="payments", alertname="ReplicaLag", severity="sev2")
    blocks = renderer.render_blocks(replica, context, matched_on="ReplicaLag")
    rendered = str(blocks)
    for step_id in replica.step_ids:
        assert f"/step done {step_id}" in rendered


# --- the detector (W5-13) -----------------------------------------------------


def test_detects_a_step_from_its_command(catalogue: list[ParsedRunbook]) -> None:
    replica = next(r for r in catalogue if "Replica" in r.name)
    message = 'running psql -h db-1 -c "SELECT now() - pg_last_xact_replay_timestamp() AS lag;"'
    assert detector.detect_in_message(replica, message) == "verify-replica-lag"


def test_ordinary_chatter_matches_no_step(catalogue: list[ParsedRunbook]) -> None:
    """A single shared token appears in several steps of the same runbook, so a
    one-token threshold would attribute a message to whichever step was checked
    first."""
    replica = next(r for r in catalogue if "Replica" in r.name)
    for message in ("morning all", "looking at the graphs", "anyone want coffee"):
        assert detector.detect_in_message(replica, message) is None


def test_a_step_with_no_command_never_matches_by_command(
    catalogue: list[ParsedRunbook],
) -> None:
    """ "Decide about failover, out loud" has nothing to grep for, and that is
    the correct outcome rather than a gap to work around -- it is why there are
    three signal sources instead of one."""
    database = next(r for r in catalogue if r.name == "Database Connection Issues")
    decision_step = next(s for s in database.steps if s.step_id == "failover-decision")
    assert detector._fingerprint(decision_step.body) == frozenset()


def test_valid_step_rejects_a_typo(catalogue: list[ParsedRunbook]) -> None:
    """A signal row for a step that does not exist would push adherence above
    1.0 and corrupt D4 in a way that looks like data rather than a bug."""
    replica = next(r for r in catalogue if "Replica" in r.name)
    assert detector.valid_step(replica, "verify-replica-lag") is True
    assert detector.valid_step(replica, "verify-replica-lagg") is False
