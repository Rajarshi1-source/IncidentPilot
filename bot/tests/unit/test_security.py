"""The trust boundary. Six cases per the plan (W1-10), plus the ones that bite.

These are the tests that matter most in week 1: `api/security.py` is the file an
interviewer is most likely to probe, and every case below corresponds to a real
failure mode rather than a coverage target.
"""

from __future__ import annotations

import time

import pytest

from incidentpilot.api.security import (
    Unauthorized,
    sign_paging,
    sign_slack,
    verify_bearer,
    verify_paging,
    verify_slack,
)

SECRET = "s3cr3t-signing-key"
BODY = b'{"alerts":[{"status":"firing"}]}'


# --- bearer (Alertmanager, deploy) -------------------------------------------


def test_bearer_accepts_the_configured_token() -> None:
    verify_bearer("Bearer good-token", "good-token")


def test_bearer_rejects_a_wrong_token() -> None:
    with pytest.raises(Unauthorized, match="bad bearer"):
        verify_bearer("Bearer wrong-token", "good-token")


def test_bearer_rejects_a_missing_header() -> None:
    with pytest.raises(Unauthorized, match="missing bearer"):
        verify_bearer(None, "good-token")


def test_bearer_rejects_a_malformed_scheme() -> None:
    """`Basic` or a bare token must not be accepted as a bearer."""
    with pytest.raises(Unauthorized, match="missing bearer"):
        verify_bearer("good-token", "good-token")
    with pytest.raises(Unauthorized, match="missing bearer"):
        verify_bearer("Basic good-token", "good-token")


def test_bearer_fails_closed_when_no_secret_is_configured() -> None:
    """A misconfiguration must reject, not wave everything through.

    An unset secret that accepts all requests is strictly worse than an
    endpoint that returns 401 until someone fixes the config.
    """
    with pytest.raises(Unauthorized, match="no bearer configured"):
        verify_bearer("Bearer anything", None)
    with pytest.raises(Unauthorized, match="no bearer configured"):
        verify_bearer("Bearer anything", "")


# --- Slack v0 -----------------------------------------------------------------


def test_slack_accepts_a_valid_signature() -> None:
    ts = str(int(time.time()))
    verify_slack(BODY, ts, sign_slack(BODY, ts, SECRET), SECRET)


def test_slack_rejects_a_tampered_body() -> None:
    """The signature covers the body; changing one byte must invalidate it."""
    ts = str(int(time.time()))
    signature = sign_slack(BODY, ts, SECRET)
    with pytest.raises(Unauthorized, match="bad signature"):
        verify_slack(BODY + b" ", ts, signature, SECRET)


def test_slack_rejects_a_stale_timestamp() -> None:
    """Replay window, not clock sync. 301 seconds old is a replay."""
    now = time.time()
    ts = str(int(now - 301))
    with pytest.raises(Unauthorized, match="stale timestamp"):
        verify_slack(BODY, ts, sign_slack(BODY, ts, SECRET), SECRET, now=now)


def test_slack_rejects_a_future_timestamp() -> None:
    """A future-dated request is as much an attack as an old one."""
    now = time.time()
    ts = str(int(now + 900))
    with pytest.raises(Unauthorized, match="stale timestamp"):
        verify_slack(BODY, ts, sign_slack(BODY, ts, SECRET), SECRET, now=now)


def test_slack_accepts_at_the_window_edge() -> None:
    """Exactly at the window is accepted; one second past it is not.

    `now` is an integer here on purpose: Slack sends whole-second timestamps,
    so a fractional `now` would put the boundary a fraction of a second the
    wrong side of the comparison and make this test flaky by construction.
    """
    now = float(int(time.time()))
    at_edge = str(int(now) - 300)
    verify_slack(BODY, at_edge, sign_slack(BODY, at_edge, SECRET), SECRET, now=now)

    just_past = str(int(now) - 301)
    with pytest.raises(Unauthorized, match="stale timestamp"):
        verify_slack(BODY, just_past, sign_slack(BODY, just_past, SECRET), SECRET, now=now)


def test_slack_rejects_missing_headers() -> None:
    ts = str(int(time.time()))
    with pytest.raises(Unauthorized, match="missing slack headers"):
        verify_slack(BODY, None, sign_slack(BODY, ts, SECRET), SECRET)
    with pytest.raises(Unauthorized, match="missing slack headers"):
        verify_slack(BODY, ts, None, SECRET)


def test_slack_rejects_a_non_numeric_timestamp() -> None:
    with pytest.raises(Unauthorized, match="malformed slack timestamp"):
        verify_slack(BODY, "not-a-number", "v0=deadbeef", SECRET)


def test_slack_signature_is_over_raw_bytes_not_reserialized_json() -> None:
    """The failure mode this guards is subtle and total.

    FastAPI will hand you a parsed body. Re-serializing it changes whitespace
    and key order, producing different bytes and therefore a different digest --
    so *every* request fails verification, with a signature that looks correct.
    """
    import json

    raw = b'{"b":2,  "a":1}'
    ts = str(int(time.time()))
    signature = sign_slack(raw, ts, SECRET)

    verify_slack(raw, ts, signature, SECRET)

    reserialized = json.dumps(json.loads(raw)).encode()
    assert reserialized != raw
    with pytest.raises(Unauthorized, match="bad signature"):
        verify_slack(reserialized, ts, signature, SECRET)


# --- paging provider, with rotation ------------------------------------------


def test_paging_accepts_a_valid_signature() -> None:
    verify_paging(BODY, sign_paging(BODY, SECRET), SECRET)


def test_paging_accepts_rotated_signature() -> None:
    """Providers send several signatures during key rotation -- one per active
    key. Accepting only the first is how a routine rotation becomes a hard
    outage of on-call callbacks.
    """
    old_key_sig = sign_paging(BODY, "previous-secret")
    current_sig = sign_paging(BODY, SECRET)

    verify_paging(BODY, f"{old_key_sig},{current_sig}", SECRET)
    verify_paging(BODY, f"{current_sig},{old_key_sig}", SECRET)
    verify_paging(BODY, f" {old_key_sig} , {current_sig} ", SECRET)


def test_paging_rejects_when_no_signature_matches() -> None:
    with pytest.raises(Unauthorized, match="bad signature"):
        verify_paging(
            BODY,
            f"{sign_paging(BODY, 'wrong-a')},{sign_paging(BODY, 'wrong-b')}",
            SECRET,
        )


def test_paging_rejects_a_missing_or_empty_header() -> None:
    with pytest.raises(Unauthorized, match="missing signature"):
        verify_paging(BODY, None, SECRET)
    with pytest.raises(Unauthorized, match="missing signature"):
        verify_paging(BODY, " , , ", SECRET)


def test_paging_fails_closed_without_a_configured_secret() -> None:
    with pytest.raises(Unauthorized, match="no paging webhook secret"):
        verify_paging(BODY, sign_paging(BODY, SECRET), None)
