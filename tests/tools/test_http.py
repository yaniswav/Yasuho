import types

import pytest

from tools.http import get_session


def test_get_session_returns_bot_owned_open_session():
    session = types.SimpleNamespace(closed=False)
    bot = types.SimpleNamespace(http_session=session)

    assert get_session(bot) is session


@pytest.mark.parametrize(
    "bot",
    [
        types.SimpleNamespace(),
        types.SimpleNamespace(http_session=None),
        types.SimpleNamespace(
            http_session=types.SimpleNamespace(closed=True)
        ),
    ],
)
def test_get_session_rejects_invalid_lifecycle(bot):
    with pytest.raises(RuntimeError, match="shared HTTP session"):
        get_session(bot)
