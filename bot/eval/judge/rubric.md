# Judge rubric — the qualities code cannot score

The judge exists because two things a good post-incident review has cannot be
computed: whether the narrative *coheres*, and whether the root-cause hypothesis
is *reasonable given the evidence*. Everything else in this harness is set
membership, token overlap and arithmetic, and everything else is gated hard.

This role is **offline only**. `models.yaml` marks it so and `ModelsConfig.role`
refuses to resolve it outside the harness. A judge on the production path would
put a second model between an incident and its postmortem, and the validator is
deterministic precisely so that nothing has to.

## What the judge is asked

Given the incident evidence and a generated PIR, score three dimensions on a
0–2 scale. Three points, not five: on a ten-incident labelled subset the
difference between a 3 and a 4 out of 5 is noise, and a scale with more
resolution than the labeller has is a scale that manufactures disagreement.

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| **Coherence** | The narrative contradicts itself or the timeline runs backwards | Readable but a reader has to reconstruct the order of events | A reader who was not there can follow what happened and when |
| **Root-cause reasonableness** | Asserts a cause the evidence does not support, or invents a mechanism | Plausible but the evidence is thin and the draft does not say so | Supported by cited evidence, **or correctly abstains** |
| **Action-item usefulness** | Generic ("improve monitoring") or aimed at someone uninvolved | Specific but not clearly derived from this incident | Specific, derived from a cited moment, and assignable |

## The rule that matters most

**Abstention scores 2 on root cause.** A PIR that says "root cause not
established" for an incident where nobody established one is *correct*, and a
judge that rewards confident invention over honest uncertainty would train the
prompt in exactly the wrong direction. `root_cause_hypothesis` is nullable in
the schema for the same reason.

## Output

```json
{"coherence": 0|1|2, "root_cause": 0|1|2, "action_items": 0|1|2, "notes": "one sentence"}
```

## Why agreement is measured and published

A judge's score is only worth what its agreement with a human is worth. The
harness scores the same ten incidents by hand and by judge and reports both the
raw agreement rate and Cohen's kappa:

```
judge_agreement: 0.82 (n=10, Cohen's kappa 0.61)
```

Kappa alongside the raw rate because raw agreement flatters a skewed label
distribution: if nine of ten incidents are "2", a judge that answers "2" every
time agrees 90% of the time and knows nothing. Kappa corrects for the agreement
you would get by chance, and 0.61 is "substantial" rather than "almost perfect"
— which is the honest reading and the reason these dimensions are **reported,
never gated**.

Stating that split out loud — hard gates deterministic, judge measured and
soft — is the point of having a judge at all.
