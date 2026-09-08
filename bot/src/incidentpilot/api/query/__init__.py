"""The read API the dashboard consumes (W8-02).

Separate from ``api/webhooks`` and ``api/slash`` because it is a different
contract with different failure semantics. A webhook that is slow drops an
alert; an analytics endpoint that is slow renders a spinner. They therefore get
different statement timeouts, different caching advice, and -- in
``DEMO_MODE`` -- different permissions.

**Read-only, structurally.** Every route here is a GET, every one calls a named
function in ``db/queries.py``, and none of them accepts a SQL fragment. The
dashboard cannot mutate anything through this surface even if it tried, which is
what makes publishing it at a URL a stranger can reach a defensible decision
rather than a hopeful one.
"""

from incidentpilot.api.query.routes import router

__all__ = ["router"]
