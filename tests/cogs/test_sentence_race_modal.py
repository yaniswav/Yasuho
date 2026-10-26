"""Sentence Race answers arrive through a modal, never by reading chat.

A modal submission carries its text without the Message Content intent; a
`wait_for("message")` content check would see empty strings once the intent
is moderation-only.
"""

import inspect
import types

from cogs.fun import games


class _Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _interaction(user_id):
    user = types.SimpleNamespace(
        id=user_id, mention=f"<@{user_id}>",
        display_avatar=types.SimpleNamespace(url="https://x/a.png"),
    )
    return types.SimpleNamespace(user=user, response=_Response())


async def test_first_exact_answer_wins_and_later_ones_are_refused():
    race = games.SentenceRaceView("the quick fox")
    first, second = _interaction(1), _interaction(2)
    await race.submit(first, "  the quick fox ")
    await race.submit(second, "the quick fox")
    assert race.winner is first.user
    assert "embed" in first.response.sent[0][1]
    assert second.response.sent[0][1].get("ephemeral") is True


async def test_a_wrong_answer_is_private_and_keeps_the_race_open():
    race = games.SentenceRaceView("the quick fox")
    player = _interaction(1)
    await race.submit(player, "the quick dog")
    assert race.winner is None
    assert player.response.sent[0][1].get("ephemeral") is True


def test_the_race_no_longer_reads_chat_messages():
    source = inspect.getsource(games.Games.sentencerace.callback)
    assert "wait_for" not in source and ".content" not in source
