"""Tests for the polite SOCRATES HTTP client (assignment requirement #1).

These prove the anti-ban behaviour *without touching the network*: a fake
session feeds canned responses and records the request headers, so we can
assert on conditional-GET validators, retry/backoff, Retry-After, and the
give-up path. Backoff sleeps are stubbed so the suite stays fast.
"""

from __future__ import annotations

import pytest

import app.socrates_client as sc
from app.config import Settings
from app.socrates_client import SocratesClient, SocratesError


class FakeResp:
    def __init__(self, status_code: int, headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.content = text.encode("utf-8")


class FakeSession:
    """Stand-in for requests.Session that returns queued responses.

    A queued item that is an Exception instance is *raised* (to simulate a
    transport error); anything else is returned. Every call's headers are
    recorded so tests can assert which conditional-GET validators were sent.
    """

    def __init__(self, queue: list):
        self._queue = list(queue)
        self.calls: list[dict] = []
        self.headers: dict = {}

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make backoff instantaneous and record the delays requested."""
    delays: list[float] = []
    monkeypatch.setattr(sc.time, "sleep", lambda s: delays.append(s))
    return delays


def _client(queue, **overrides) -> tuple[SocratesClient, FakeSession]:
    settings = Settings(
        socrates_source_url="https://example.test/socrates.csv",
        http_max_retries=overrides.pop("http_max_retries", 3),
        http_backoff_base_seconds=overrides.pop("http_backoff_base_seconds", 1.0),
        http_timeout_seconds=5.0,
        **overrides,
    )
    client = SocratesClient(settings)
    session = FakeSession(queue)
    client._session = session  # swap in the fake after construction
    return client, session


def test_200_returns_ok_and_caches_validators():
    client, _ = _client(
        [FakeResp(200, {"ETag": '"abc"', "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"}, "csv-body")]
    )
    result = client.fetch()
    assert result.status == "ok"
    assert result.http_status == 200
    assert result.body == "csv-body"
    assert result.etag == '"abc"'
    assert result.bytes_downloaded == len(b"csv-body")


def test_conditional_get_sends_validators_on_next_fetch():
    client, session = _client(
        [
            FakeResp(200, {"ETag": '"v1"', "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"}, "body"),
            FakeResp(304),
        ]
    )
    client.fetch()  # primes the validator cache
    second = client.fetch()
    assert second.status == "not_modified"
    assert second.body is None
    # The second request must carry the validators we learned from the first.
    sent = session.calls[1]["headers"]
    assert sent.get("If-None-Match") == '"v1"'
    assert "If-Modified-Since" in sent


def test_304_is_not_an_error():
    client, _ = _client([FakeResp(304)])
    result = client.fetch()
    assert result.status == "not_modified"
    assert result.http_status == 304


def test_429_then_200_retries_with_backoff(_no_sleep):
    client, session = _client([FakeResp(429), FakeResp(200, {}, "ok-body")])
    result = client.fetch()
    assert result.status == "ok"
    assert len(session.calls) == 2  # retried once
    assert _no_sleep, "expected a backoff sleep between the 429 and the retry"


def test_retry_after_header_is_honoured(_no_sleep):
    client, _ = _client([FakeResp(429, {"Retry-After": "7"}), FakeResp(200, {}, "x")])
    client.fetch()
    assert 7.0 in _no_sleep  # slept exactly the server-requested interval


def test_5xx_exhausts_retries_then_raises():
    # Always 503: with max_retries=2 that is 3 attempts, then give up.
    client, session = _client([FakeResp(503)] * 3, http_max_retries=2)
    with pytest.raises(SocratesError):
        client.fetch()
    assert len(session.calls) == 3


def test_transport_error_is_retried_then_raises():
    import requests

    client, session = _client(
        [requests.ConnectionError("boom")] * 3, http_max_retries=2
    )
    with pytest.raises(SocratesError):
        client.fetch()
    assert len(session.calls) == 3


def test_4xx_client_error_is_not_retried():
    client, session = _client([FakeResp(404)], http_max_retries=3)
    result = client.fetch()
    assert result.status == "error"
    assert result.http_status == 404
    assert len(session.calls) == 1  # no pointless retries on a 404
