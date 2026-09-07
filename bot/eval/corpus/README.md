# The corpus — 40 incidents, 11 buckets

## These are authored fixtures, not captured production traffic

There is no year of real incidents behind this project. Saying so here, in the
first line, rather than letting a reader assume otherwise is the same discipline
as the rest of the repository: a corpus that implied provenance it does not have
would be the same category of claim as a gate that never runs.

What they *are* is shaped like real incidents — eleven buckets chosen for the
specific failure each one protects, labels written the way a reviewer would
write them, and four buckets the system is expected to score badly on. **A
fixture the system passes by construction protects nothing.**

Forty is a small corpus. It is wide enough to catch a structural regression and
far too narrow for a confidence interval, and the report footer says so on every
run rather than leaving it to this file.

## The buckets

| Bucket | n | ids | What it protects |
|---|---|---|---|
| `real` | 12 | 201–212 | Realistic language and mess across the service graph |
| `storm` | 4 | 213–216 | Storm compression, D3 (5 / 12 / 40 / 40 alerts) |
| `flapping` | 3 | 217–219 | **No PIR must be produced** |
| `reopened` | 2 | 220–221 | The state machine's most-used branch |
| `abandoned` | 2 | 222–223 | Terminal branch; no transcript to ground anything in |
| `metrics_unavailable` | 3 | 224–226 | Impact says *unavailable*, never estimates |
| `provider_failure` | 3 | 227–229 | The fallback ladder: timeout, invalid JSON, total loss |
| `unknown_root_cause` | 3 | 230–232 | The model must **abstain**, not invent |
| `code_switched` | 4 | 233–236 | A known weakness, measured while it fails |
| `very_long` | 2 | 237–238 | Context assembly and truncation (420+ messages) |
| `adversarial` | 2 | 239–240 | Validator resistance to a planted reference |

## The four buckets that exist to fail

**`code_switched`** scores 0.667 on timeline F1 against 1.0 elsewhere. The
intent rules were written in English and a channel that is half English and half
Hindi is normal on an Indian engineering team. This is measured *while it fails*,
because a measured gap is a much better answer to "what is weakest right now?"
than an unmeasured one — and because this bucket is what will prove the fix when
layer 2 of the classifier ladder gets tuned.

**`flapping`** and **`abandoned`** must produce no PIR at all. The opposite
failure is invisible: a product that writes a thoughtful postmortem about an
alert that flapped twice and resolved itself *looks like it is working*.

**`unknown_root_cause`** requires abstention. `root_cause_hypothesis` is nullable
in the schema precisely so "nobody established a cause" is representable, and the
judge rubric scores abstention 2/2 — a judge that rewarded confident invention
over honest uncertainty would train the prompt in exactly the wrong direction.

## The adversarial bucket

Both fixtures plant `msg:1757000000.000100` in the transcript. It is well-formed
under the evidence grammar, plausible, and absent from the store.

- **239** records a model that ignores it. Clean draft, first attempt.
- **240** records a model that **takes the bait**. The first response cites the
  planted ID, the validator rejects it as `fabricated`, the generator retries,
  and the second response is clean. One document is published, never a partial.

That second one is D1's acceptance criterion replayed end to end, and it is the
cheapest concrete test of prompt-injection resistance in the project. Grep the
run for `pir.validation_rejected` on incident 240 to watch it happen.

## Ground truth lives inside the recording

Every file ends with a `ground_truth` line. Labels are **not** in a sidecar
spreadsheet, because two files drift and one does not — and a label that no
longer matches the incident it scores permanently distorts every future
comparison with nothing in CI to notice.

Labels are written at **intent granularity**, not word granularity, and action
items are labelled as the *set* a competent reviewer would extract — deliberately
phrased differently from what the drafts say, so `action_item_recall` measures
recall rather than string equality. The corpus-wide figure is 0.863, not 1.0, and
the headroom is real.

## The format

One JSONL file per incident. A `{"format": 1}` header, then one line per
interaction carrying `t` (seconds from incident start) and a `kind`.

`t` is relative on purpose: absolute timestamps would make every fixture expire,
because a window computed against `now` behaves differently in January than it
did in September, and a corpus whose results drift with the calendar is not a
baseline. The replayer's virtual clock reconstitutes absolute time from a fixed
start.

The version header exists because the listed failure mode of an eval harness is
not that it breaks — it is that the corpus rots. A format number makes corpus
migration a normal task with a normal diff.

## Regenerating

```bash
make eval-record        # uv run python -m eval.corpus._generate
```

`_generate.py` is committed alongside its output. When the impact queries or the
evidence grammar change, the corpus must be rebuilt — the queries are generated
from the product's own `IMPACT_QUERIES` and `window_for`, so a corpus written by
hand would drift from the code the first time a label selector changed and the
replay fake would start raising *no recorded response*.

Generation is fully deterministic: no clock, no `random`, no `uuid`. The same
script produces byte-identical files on every machine, which is what makes a
corpus diff reviewable.

## What is in here that must never be

No credentials. `test_no_recording_carries_a_credential` scans every file for
anything shaped like a bot token, an API key or an AWS key, and the recorder
drops credential-shaped fields *at capture* rather than at read. "We redact when
we load it" is a promise the filesystem does not keep — this directory is
committed to git, and git does not forget.

## The baseline

`manifest.json` holds both the bucket map and the committed baseline. The
baseline moves only when a human calls `eval.gate.promote()` after reading the
numbers. CI never promotes: a gate that promotes its own baseline on green
ratchets to whatever it last measured and can never detect a slow regression —
each run 0.001 worse than the last, each run passing.
