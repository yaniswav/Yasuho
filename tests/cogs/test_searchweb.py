"""Tests for the wikipedia timeout proxy in cogs/utility/searchweb.py.

The wikipedia library issues requests.get() with no timeout; searchweb wraps
its requests reference so a hung upstream cannot tie up an executor thread
forever. These tests are hermetic (no network): they check the proxy's
behaviour and that the wikipedia module actually picked it up on import.
"""

import wikipedia

from cogs.utility import searchweb


def test_timeout_requests_injects_default(monkeypatch):
    captured = {}

    def fake_get(*args, **kwargs):
        captured.update(kwargs)
        return "resp"

    monkeypatch.setattr(searchweb.requests, "get", fake_get)
    proxy = searchweb._TimeoutRequests(15)
    assert proxy.get("http://example/api") == "resp"
    assert captured["timeout"] == 15


def test_timeout_requests_preserves_explicit_timeout(monkeypatch):
    captured = {}

    def fake_get(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(searchweb.requests, "get", fake_get)
    searchweb._TimeoutRequests(15).get("http://example/api", timeout=3)
    assert captured["timeout"] == 3


def test_timeout_requests_forwards_other_attributes():
    proxy = searchweb._TimeoutRequests(15)
    # anything that is not get() falls through to the real requests module.
    assert proxy.exceptions is searchweb.requests.exceptions


def test_wikipedia_module_uses_the_timeout_proxy():
    assert isinstance(wikipedia.wikipedia.requests, searchweb._TimeoutRequests)
