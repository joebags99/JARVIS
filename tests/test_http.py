"""Tests for the shared HTTP retry policy (integrations/_http.py)."""

from __future__ import annotations

import pytest
import requests

from integrations import _http


def test_backoff_delay_sequence():
    assert [_http.backoff_delay(i) for i in (1, 2, 3)] == [0.5, 1.0, 2.0]


@pytest.mark.parametrize(
    "status,idempotent,expected",
    [
        (429, False, True),
        (502, False, True),
        (503, False, True),
        (504, False, True),
        (500, True, True),    # ambiguous 500 only retried for idempotent calls
        (500, False, False),  # …never for writes
        (404, True, False),
        (401, True, False),
        (200, True, False),
        (None, True, False),
    ],
)
def test_should_retry_status(status, idempotent, expected):
    assert _http.should_retry_status(status, idempotent) is expected


class _FakeResp:
    def __init__(self, status: int):
        self.status_code = status
        self.headers: dict = {}
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_request_with_retries_retries_transient(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return _FakeResp(503 if calls["n"] == 1 else 200)

    monkeypatch.setattr(_http.requests, "request", fake_request)
    monkeypatch.setattr(_http.time, "sleep", lambda *_: None)

    resp = _http.request_with_retries("GET", "http://x/y")
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_request_with_retries_raises_non_transient(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return _FakeResp(404)

    monkeypatch.setattr(_http.requests, "request", fake_request)
    monkeypatch.setattr(_http.time, "sleep", lambda *_: None)

    with pytest.raises(requests.HTTPError):
        _http.request_with_retries("GET", "http://x/y")
    assert calls["n"] == 1  # no retry on a 404


def test_request_with_retries_gives_up_after_max(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return _FakeResp(503)

    monkeypatch.setattr(_http.requests, "request", fake_request)
    monkeypatch.setattr(_http.time, "sleep", lambda *_: None)

    with pytest.raises(requests.HTTPError):
        _http.request_with_retries("GET", "http://x/y")
    assert calls["n"] == _http.MAX_ATTEMPTS
