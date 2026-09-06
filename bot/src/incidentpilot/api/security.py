"""The webhook trust boundary. Three sources, three trust models.

    Alertmanager  bearer + mTLS + NetworkPolicy   -- it cannot sign at all
    Paging        HMAC-SHA256, multi-signature    -- rotation-aware
    Slack         v0 HMAC over the RAW body       -- 300s replay window
    Deploy (CI)   bearer + repo allowlist         -- evidence in a PIR

The interview sentence: *"Three sources, three trust models. Slack signs,
PagerDuty signs with rotation support, Alertmanager doesn't sign at all -- so
that endpoint gets mTLS plus a bearer plus a NetworkPolicy, because it's the one
that can't prove who it is."*

Every comparison uses ``hmac.compare_digest``. A naive ``==`` on a secret leaks
its prefix through timing, and a webhook endpoint is reachable by anyone who can
route to the pod.
"""

from __future__ import annotations

import hashlib
import hmac
import time


class Unauthorized(Exception):
    """Request failed verification. Callers return 401 and log the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_bearer(header: str | None, expected: str | None) -> None:
    """Constant-time bearer check.

    Used for Alertmanager and for CI deploy events. B-02: Alertmanager does NOT
    sign its payloads -- ``http_config`` offers basic auth, bearer tokens,
    OAuth2 and TLS, and nothing more. There is no HMAC header to validate, so
    claiming signature validation on that endpoint is factually wrong and an
    interviewer who has configured Alertmanager will catch it. The trust model
    is bearer + mTLS + a NetworkPolicy allowlist: the source cannot prove
    identity, so the network does it on its behalf.
    """
    if not expected:
        # Refuse rather than accept. A misconfiguration must fail closed --
        # an unset secret that waves everything through is the worst outcome.
        raise Unauthorized("no bearer configured for this endpoint")
    if not header or not header.startswith("Bearer "):
        raise Unauthorized("missing bearer")
    if not hmac.compare_digest(header[7:], expected):
        raise Unauthorized("bad bearer")


def verify_slack(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str | None,
    *,
    max_age_s: int = 300,
    now: float | None = None,
) -> None:
    """Slack v0 signature over ``v0:{timestamp}:{raw_body}``.

    Two things are easy to get wrong and expensive to debug:

    1. The HMAC is over the **raw bytes**. FastAPI will happily hand you a
       parsed body; re-serializing it produces different bytes and therefore a
       different digest, so *every* request fails verification. Read the body
       with ``await request.body()`` and parse it yourself.
    2. The timestamp check is a replay window, not a clock sync. Reject anything
       older than 300 s in either direction -- a future-dated timestamp is just
       as much an attack as an old one.

    ``now`` is injectable so the replay window is testable without freezing the
    process clock.
    """
    if not secret:
        raise Unauthorized("no slack signing secret configured")
    if not timestamp or not signature:
        raise Unauthorized("missing slack headers")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise Unauthorized("malformed slack timestamp") from exc

    current = time.time() if now is None else now
    if abs(current - sent_at) > max_age_s:
        raise Unauthorized("stale timestamp")

    base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise Unauthorized("bad signature")


def verify_paging(
    raw_body: bytes,
    signature_header: str | None,
    secret: str | None,
    *,
    prefix: str = "v1=",
) -> None:
    """Paging-provider HMAC, accepting any one of several signatures.

    Providers send **multiple** comma-separated signatures during secret
    rotation -- one per active key. Accepting only the first is how key rotation
    turns into a hard outage of on-call callbacks, at the exact moment nobody
    wants to debug a webhook.
    """
    if not secret:
        raise Unauthorized("no paging webhook secret configured")
    if not signature_header:
        raise Unauthorized("missing signature")

    mine = prefix + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    candidates = [part.strip() for part in signature_header.split(",") if part.strip()]
    if not candidates:
        raise Unauthorized("missing signature")
    if not any(hmac.compare_digest(mine, candidate) for candidate in candidates):
        raise Unauthorized("bad signature")


def sign_paging(raw_body: bytes, secret: str, *, prefix: str = "v1=") -> str:
    """Produce a signature. Test helper and fixture generator, not a hot path."""
    return prefix + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def sign_slack(raw_body: bytes, timestamp: str, secret: str) -> str:
    """Produce a Slack v0 signature. Test helper and fixture generator."""
    base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
