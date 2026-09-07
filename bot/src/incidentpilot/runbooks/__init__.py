"""Runbooks: parsed from markdown, matched to the root signal, rendered to
Block Kit, and instrumented so the system can eventually tell you which steps
nobody ever runs (D4).

The loop is deliberately **not** closed yet. ``db/queries.py`` ships the
corrected dead-step query and ``detector.py`` captures the signals it reads, but
the auto-PR job that would act on them is deferred post-MVP (E-3): the query
filters on five incidents per runbook and eight weeks will not produce them.
Saying "the signal capture works and the loop has not closed" is the honest
version; claiming a closed loop that has never closed is what a follow-up
question exposes.
"""
