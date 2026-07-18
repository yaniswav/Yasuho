import types

import pytest

from cogs.anilist import feed


class _Response:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def json(self):
        return self.payload


class _Request:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    closed = False

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Request(self.response)


async def test_authed_graphql_uses_shared_session_and_bearer_header():
    session = _Session(_Response(200, {"data": {"ok": True}}))
    bot = types.SimpleNamespace(http_session=session)

    result = await feed._authed_graphql(
        bot, "secret-token", "query Test { ok }", {"id": 42}
    )

    assert result == {"data": {"ok": True}}
    assert len(session.calls) == 1
    _url, kwargs = session.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"
    assert kwargs["json"]["variables"] == {"id": 42}


async def test_authed_graphql_preserves_rate_limit_signal():
    session = _Session(
        _Response(429, {}, headers={"Retry-After": "12"})
    )
    bot = types.SimpleNamespace(http_session=session)

    with pytest.raises(feed._RateLimited) as caught:
        await feed._authed_graphql(bot, "token", "query { ok }", {})

    assert caught.value.retry_after == 12
