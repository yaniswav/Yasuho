"""Unit tests for the shared View bases (tools.views).

Focused on the one behaviour every Components V2 surface inherits and none of
them restates: the TIMEOUT edit. A LayoutView carries its text INSIDE the view,
so re-sending the view re-sends every TextDisplay - and several layouts hold raw
``<@id>`` / ``<@&id>`` tokens (the hall-of-fame podium, the seasons panel's
champion role, the reminders card). discord.py folds the CLIENT default
(core.Yasuho: users=True) into any edit that does not pass allowed_mentions, so
an unsuppressed timeout edit would PING those members three minutes after the
fact for a message nobody touched. The base must therefore always pass
``AllowedMentions.none()``.

No network, no Discord, no DB: a recording fake message is all the timeout path
needs.
"""

import discord

from tools.views import AuthorLayoutView, AuthorView


class _FakeMessage:
    def __init__(self):
        self.edits = []
        self.raises = None

    async def edit(self, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.edits.append(kwargs)


class _Card(AuthorLayoutView):
    """A minimal layout with one button, the shape every panel shares."""

    def __init__(self, author_id=1):
        super().__init__(author_id)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("<@42> won the season"))
        container.add_item(
            discord.ui.ActionRow(discord.ui.Button(label="Prev"))
        )
        self.add_item(container)


def _http_exc():
    return discord.HTTPException(
        type("R", (), {"status": 500, "reason": "err"})(), "boom"
    )


async def test_layout_timeout_edit_suppresses_every_mention():
    view = _Card()
    view.message = _FakeMessage()

    await view.on_timeout()

    assert len(view.message.edits) == 1
    kwargs = view.message.edits[0]
    assert kwargs["view"] is view
    mentions = kwargs["allowed_mentions"]
    # none(): no @everyone, no roles, no users, no replied-user - a greyed-out
    # panel notifies nobody.
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_layout_timeout_still_disables_every_control():
    view = _Card()
    view.message = _FakeMessage()

    await view.on_timeout()

    buttons = [
        child
        for child in view.walk_children()
        if isinstance(child, discord.ui.Button)
    ]
    assert buttons and all(button.disabled for button in buttons)


async def test_layout_timeout_without_a_message_is_a_quiet_noop():
    view = _Card()  # never sent, so self.message is None
    await view.on_timeout()  # must not raise


async def test_layout_timeout_swallows_an_http_failure():
    view = _Card()
    view.message = _FakeMessage()
    view.message.raises = _http_exc()

    await view.on_timeout()  # a dead message must not crash the timeout task

    assert view.message.edits == []


async def test_plain_authorview_timeout_still_edits_the_message():
    """The non-layout base is untouched by the mention fix: a plain View's
    content lives on the MESSAGE, not in the view, so its timeout edit does not
    resend any text and has nothing to re-parse."""
    view = AuthorView(1)
    view.add_item(discord.ui.Button(label="ok"))
    view.message = _FakeMessage()

    await view.on_timeout()

    assert len(view.message.edits) == 1
    assert view.message.edits[0]["view"] is view
    assert all(child.disabled for child in view.children)
