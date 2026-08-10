"""Mention-policy tests for reminder delivery (cogs/community/reminders.py).

A reminder body is free text stored on the author's behalf and replayed later
into a channel by the bot. A RECURRING one replays it on a schedule the author
picks, which under default mentions turns ``?remind daily @victim`` into a
scheduled harassment tool aimed at a third party who never opted in.

The template writes exactly ONE mention of its own - the author's - so the
delivery's allow-list is exactly that one id and nothing the body says can add
to it. Assertions are made on the AllowedMentions handed to ``channel.send``
(its ``to_dict()`` wire form), because the body text itself is expected to keep
its raw characters; what must be impossible is Discord resolving them.
"""

import datetime
import types

import discord
import pytest

from cogs.community.reminders import Reminder, RemindersCard, RemindModal

UTC = datetime.timezone.utc

AUTHOR_ID = 5
VICTIM_ID = 777
CHANNEL_ID = 9
DAY = 86400


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class _Channel:
    """Unlike the delivery fakes in test_reminders_recurring, this one keeps the
    KEYWORDS - the mention policy lives there, not in the content."""

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def _cog(channel):
    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    bot = types.SimpleNamespace(
        db_pool=None,
        loop=types.SimpleNamespace(create_task=_create_task),
        get_channel=lambda _cid: channel,
    )
    return Reminder(bot)


def _row(**extra):
    payload = {"author_id": AUTHOR_ID, "channel_id": CHANNEL_ID, "message": "milk"}
    payload.update(extra)
    return {
        "id": 1,
        "event": "reminder",
        "created": datetime.datetime.now(UTC) - datetime.timedelta(days=1),
        "extra": payload,
    }


def _wire(kwargs):
    allowed = kwargs.get("allowed_mentions")
    assert isinstance(allowed, discord.AllowedMentions), (
        "delivery must carry an EXPLICIT allowed_mentions, not the bot default"
    )
    return allowed.to_dict()


def _assert_only(wire, permitted_ids):
    assert "everyone" not in wire.get("parse", [])
    assert "roles" not in wire.get("parse", [])
    assert wire.get("roles", []) == []
    # "users" in parse means "resolve every mention in the body" - the hole.
    assert "users" not in wire.get("parse", [])
    assert set(wire.get("users", [])) == set(permitted_ids)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_a_reminder_body_cannot_ping_the_third_party_it_names():
    channel = _Channel()
    cog = _cog(channel)

    await cog.call_timer(_row(message=f"@everyone <@{VICTIM_ID}> wake up"))

    assert len(channel.sent) == 1, "the reminder must still be delivered"
    content, kwargs = channel.sent[0]
    assert f"<@{VICTIM_ID}>" in content  # text verbatim...
    wire = _wire(kwargs)
    _assert_only(wire, {AUTHOR_ID})  # ...but inert
    assert VICTIM_ID not in wire.get("users", [])


async def test_here_and_everyone_in_a_body_stay_plain_text():
    channel = _Channel()
    cog = _cog(channel)

    await cog.call_timer(_row(message="@here @everyone standup"))

    _assert_only(_wire(channel.sent[0][1]), {AUTHOR_ID})


async def test_a_role_mention_in_a_body_stays_plain_text():
    channel = _Channel()
    cog = _cog(channel)

    await cog.call_timer(_row(message="<@&424242> get in here"))

    _assert_only(_wire(channel.sent[0][1]), {AUTHOR_ID})


async def test_the_authors_own_mention_still_resolves():
    """The point of the reminder is that it reaches its author, so the one
    mention the template itself writes must survive the tightening."""
    channel = _Channel()
    cog = _cog(channel)

    await cog.call_timer(_row())

    content, kwargs = channel.sent[0]
    assert content.startswith(f"<@{AUTHOR_ID}>,")
    assert _wire(kwargs).get("users") == [AUTHOR_ID]


async def test_a_recurring_series_carries_the_policy_on_every_occurrence():
    """The abuse is in the repetition: each replay of the same stored body must
    be guarded, not just the first delivery."""
    channel = _Channel()
    cog = _cog(channel)
    row = _row(message=f"<@{VICTIM_ID}>", repeat_seconds=DAY)

    for _i in range(3):
        await cog.call_timer(row)

    assert len(channel.sent) == 3
    for _content, kwargs in channel.sent:
        _assert_only(_wire(kwargs), {AUTHOR_ID})


async def test_an_author_id_stored_as_a_string_still_resolves():
    """``extra`` is JSON: a row written by an older/hand-edited path can hold
    the id as text, and that must not silently mute the author's own ping."""
    channel = _Channel()
    cog = _cog(channel)

    await cog.call_timer(_row(author_id=str(AUTHOR_ID)))

    assert _wire(channel.sent[0][1]).get("users") == [AUTHOR_ID]


async def test_a_corrupt_author_id_costs_the_mention_never_the_delivery():
    """The row is already deleted by the time delivery runs (at-most-once), so
    a garbage id must degrade to "nobody is pinged", not raise."""
    channel = _Channel()
    cog = _cog(channel)

    await cog.call_timer(_row(author_id="banana", message=f"<@{VICTIM_ID}>"))

    assert len(channel.sent) == 1
    _assert_only(_wire(channel.sent[0][1]), set())


# ---------------------------------------------------------------------------
# The listing card
# ---------------------------------------------------------------------------


def _card_reminders(n, message="hi"):
    return [
        {
            "id": i,
            "expires": datetime.datetime.now(UTC) + datetime.timedelta(minutes=i + 1),
            "channel_id": CHANNEL_ID,
            "message": message,
            "event": "reminder",
        }
        for i in range(1, n + 1)
    ]


async def test_paging_the_card_re_renders_with_mentions_suppressed(make_interaction):
    """The card quotes stored bodies in a TextDisplay, which resolves mentions.
    The first post suppresses them; every re-render must do the same, or turning
    a page is a second, unguarded way to publish the same text."""
    card = RemindersCard(None, AUTHOR_ID, _card_reminders(40, f"<@{VICTIM_ID}>"), False)
    interaction = make_interaction()

    await card._next(interaction)

    assert len(interaction.edits) == 1
    _assert_only(_wire(interaction.edits[0][1]), set())


async def test_cancelling_from_the_card_re_renders_with_mentions_suppressed(
    make_interaction,
):
    class _Cog:
        async def cancel_reminder(self, _reminder_id, _author_id):
            return True

    card = RemindersCard(
        _Cog(), AUTHOR_ID, _card_reminders(3, f"@everyone <@{VICTIM_ID}>"), False
    )
    interaction = make_interaction()

    await card._cancel(interaction, 1)

    assert len(interaction.edits) == 1
    _assert_only(_wire(interaction.edits[0][1]), set())


# ---------------------------------------------------------------------------
# The modal's own confirmation echo
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_inputs(monkeypatch):
    """``TextInput.value`` is a read-only property fed by the submit payload;
    point it at what the test typed instead."""
    monkeypatch.setattr(
        discord.ui.TextInput,
        "value",
        property(lambda self: getattr(self, "_test_value", "")),
        raising=False,
    )
    yield


def _modal(body):
    """A submitted RemindModal whose cog stub accepts everything.

    The modal is the ONE surface that stores the body verbatim (the prefix
    command runs its own through ``clean_content``), so its acknowledgement is
    the one echo that carries raw user text back out.
    """

    async def _get_tzinfo(_user_id):
        return UTC

    async def _pending_count(_author_id):
        return 0

    async def _create(_dt, **_kwargs):
        return 1

    cog = types.SimpleNamespace(
        get_tzinfo=_get_tzinfo,
        _pending_reminder_count=_pending_count,
        create_reminder_timer=_create,
    )
    modal = RemindModal(cog, CHANNEL_ID, AUTHOR_ID)
    modal.when_input._test_value = "10m"
    modal.message_input._test_value = body
    modal.repeat_input._test_value = ""
    return modal


async def test_the_modal_confirmation_echo_cannot_ping_what_the_body_names(
    make_interaction, patched_inputs
):
    """The acknowledgement quotes the body straight back. Nothing in an "okay,
    noted" needs to notify anyone, and the reminder is not even due yet - so a
    ping here would be a mention the author never earned and the target never
    opted into, fired at submit time."""
    modal = _modal(f"@everyone @here <@{VICTIM_ID}> <@&424242> standup")
    interaction = make_interaction()
    interaction.created_at = datetime.datetime.now(UTC)

    await modal.on_submit(interaction)

    assert len(interaction.sent) == 1
    args, kwargs = interaction.sent[0]
    assert f"<@{VICTIM_ID}>" in args[0]  # text verbatim...
    _assert_only(_wire(kwargs), set())  # ...but every token inert
    assert kwargs["ephemeral"] is True
