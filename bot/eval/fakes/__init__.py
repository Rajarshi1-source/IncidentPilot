"""Replay-side adapters (W7-03, W7-04).

These are not the application's fakes. ``adapters/*/fake.py`` are *in-memory
implementations* -- they invent plausible behaviour so a clean clone runs with no
credentials. These replay a **recording**: the same query returns the same
recorded response it returned in production, and an interaction that was never
recorded is an error rather than an invention.

The distinction matters. A harness whose fakes invent answers measures the
fakes. A harness whose fakes replay measures the system.
"""

from eval.fakes.chat import ReplayChat
from eval.fakes.llm import ReplayLLM
from eval.fakes.metrics import ReplayMetrics
from eval.fakes.paging import ReplayPaging

__all__ = ["ReplayChat", "ReplayLLM", "ReplayMetrics", "ReplayPaging"]
