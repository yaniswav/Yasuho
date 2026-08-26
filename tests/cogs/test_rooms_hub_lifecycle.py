"""What a hub create / remove is allowed to CLAIM when Discord refuses.

Two defects lived here, both of them the same lie in two directions.

``_remove_hub`` deletes the trigger channel, then the category and its children,
and its config drop must happen whatever Discord answers - a hub stuck in the
settings behind a refused delete would be worse than a leftover channel. That
best-effort is RIGHT. What was wrong was reporting an unqualified success on top
of it: with every deletion refused, both callers still said "Removed the hub"
while the category sat in the user's channel list, and the swallowed refusals
went to log.debug - debug noise for something the user can see with their own
eyes.

``_add_hub`` creates a category and then a trigger channel in two round trips.
When the second one failed, the first was left behind: "something went wrong"
plus a silent, empty category nobody asked for.

So these tests pin the OUTCOME, not the happy path: which channels are really
gone, which are still there, that a rollback happens, and that a rollback which
fails ITSELF is said out loud. The channels are subclasses of the real discord.py
classes so the ``isinstance`` dispatch under test is the real one; no Discord, no
DB, no bot.

The last two tests are the other half of the story: the DISCORD panel lied in
exactly the same way the dashboard did, so both surfaces are pinned here.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import logging
import types
from collections import defaultdict

import discord
import pytest

from cogs.config import rooms, rooms_config
from tools import autoroom

CATEGORY_ID = 444
TRIGGER_ID = 555
ROOM_ID = 666


FORBIDDEN = 403
NOT_FOUND = 404
SERVER_ERROR = 500
# Not an HTTP status and not a discord exception at all: the call went out over
# aiohttp and the transport gave up. From the user's side the channel is exactly
# as present as after a 403, so the cog has to classify it the same way instead
# of letting it propagate.
TRANSPORT_FLAKE = "transport"


def _error(status, reason):
    """A real discord.py error of the class the cog's handlers catch."""
    if status == TRANSPORT_FLAKE:
        return asyncio.TimeoutError("no answer from Discord")
    response = types.SimpleNamespace(status=status, reason=reason)
    if status == 403:
        return discord.Forbidden(response, "missing permissions")
    if status == 404:
        return discord.NotFound(response, "unknown channel")
    return discord.HTTPException(response, "server error")


# ---------------------------------------------------------------------------
# Fakes: real discord channel types (for isinstance) + a recording delete()
# ---------------------------------------------------------------------------
class _Deleting:
    def __init__(self, channel_id, *, name="channel", guild=None, refuse=None):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.deletes = 0
        self._refuse = refuse  # an HTTP status to raise, or None to succeed

    async def delete(self, *args, **kwargs):
        self.deletes += 1
        if self._refuse is not None:
            raise _error(self._refuse, "refused")


class _Voice(_Deleting, discord.VoiceChannel):
    pass


class _Category(_Deleting, discord.CategoryChannel):
    """A category with a settable child list (the real one is a property)."""

    def __init__(self, channel_id, *, children=(), **kwargs):
        super().__init__(channel_id, **kwargs)
        self._children = list(children)

    @property
    def channels(self):
        return list(self._children)


class _Guild:
    """Just enough guild for the two methods under test.

    ``create_category`` / ``create_voice_channel`` raise whatever the test
    seeded, and every channel they hand back is recorded so a rollback (or a
    missing one) is visible.
    """

    def __init__(
        self,
        *,
        guild_id=100,
        channels=(),
        category_refuses=None,
        trigger_refuses=None,
        rollback_refuses=None,
    ):
        self.id = guild_id
        self.channels = list(channels)
        self.categories = [c for c in self.channels if isinstance(c, _Category)]
        self._by_id = {c.id: c for c in self.channels}
        self.created = []
        self._category_refuses = category_refuses
        self._trigger_refuses = trigger_refuses
        self._rollback_refuses = rollback_refuses

    def get_channel(self, channel_id):
        return self._by_id.get(channel_id)

    async def create_category(self, name):
        if self._category_refuses is not None:
            raise _error(self._category_refuses, "refused")
        category = _Category(
            CATEGORY_ID, name=name, guild=self, refuse=self._rollback_refuses
        )
        self.created.append(category)
        return category

    async def create_voice_channel(self, name, category=None):
        if self._trigger_refuses is not None:
            raise _error(self._trigger_refuses, "refused")
        channel = _Voice(TRIGGER_ID, name=name, guild=self)
        self.created.append(channel)
        return channel


def _hub(hub_id="abc12345", label="Ranked"):
    return {
        "id": hub_id,
        "label": label,
        "category_id": CATEGORY_ID,
        "hub_channel_id": TRIGGER_ID,
        "template": "{user}'s room",
        "user_limit": 0,
        "max_rooms": 20,
        "private": False,
    }


def _cog(hubs=()):
    """A TemporaryRooms with its DB seams stubbed and its in-memory maps live."""
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)
    cog._hub_index = {}
    cog._active = defaultdict(set)
    cog._room_owners = {}
    cog._room_views = {}
    cog.saved = []
    stored = [dict(hub) for hub in hubs]

    async def _load_hubs(guild_id):
        return [dict(hub) for hub in stored]

    async def _save_hubs(guild_id, new_hubs):
        cog.saved.append((guild_id, [dict(hub) for hub in new_hubs]))
        return new_hubs

    cog._load_hubs = _load_hubs
    cog._save_hubs = _save_hubs
    return cog


def _live_hub_guild(*, trigger_refuses=None, room_refuses=None, category_refuses=None):
    """A guild holding one hub: category > (trigger, one live temp room)."""
    trigger = _Voice(TRIGGER_ID, name="Join to create", refuse=trigger_refuses)
    room = _Voice(ROOM_ID, name="someone's room", refuse=room_refuses)
    category = _Category(
        CATEGORY_ID,
        name="RANKED",
        children=[trigger, room],
        refuse=category_refuses,
    )
    guild = _Guild(channels=[category, trigger, room])
    for channel in (trigger, room, category):
        channel.guild = guild
    return guild, trigger, room, category


# ---------------------------------------------------------------------------
# _remove_hub: the config always goes, the CLAIM must match what Discord did
# ---------------------------------------------------------------------------
async def test_remove_hub_says_it_is_gone_when_everything_was_deleted():
    guild, trigger, room, category = _live_hub_guild()
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert isinstance(outcome, autoroom.HubRemoval)
    assert outcome.removed is True
    assert outcome.failed == ()  # the only proof of "gone"
    assert set(outcome.deleted) == {TRIGGER_ID, ROOM_ID, CATEGORY_ID}
    assert outcome.message == "Removed the **Ranked** hub."
    assert cog.saved == [(100, [])]


async def test_remove_hub_reports_the_channels_discord_refused():
    """The category survived: the answer must not read as an unqualified done."""
    guild, trigger, room, category = _live_hub_guild(category_refuses=FORBIDDEN)
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert outcome.failed == (CATEGORY_ID,)  # still in front of the user
    assert set(outcome.deleted) == {TRIGGER_ID, ROOM_ID}
    assert "could not be deleted" in outcome.message
    assert "Removed the **Ranked** hub." != outcome.message


async def test_remove_hub_still_drops_the_config_when_every_delete_fails():
    """The best-effort itself is the RIGHT design and must stay best-effort.

    A hub kept in the settings because Discord refused would keep spinning up
    rooms from a config the user has already deleted.
    """
    guild, trigger, room, category = _live_hub_guild(
        trigger_refuses=FORBIDDEN, room_refuses=FORBIDDEN, category_refuses=FORBIDDEN
    )
    cog = _cog([_hub(), _hub(hub_id="other001", label="Casual")])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert cog.saved == [(100, [_hub(hub_id="other001", label="Casual")])]
    assert outcome.removed is True  # the config half DID happen
    assert set(outcome.failed) == {TRIGGER_ID, ROOM_ID, CATEGORY_ID}
    assert outcome.deleted == ()


async def test_remove_hub_counts_a_404_as_gone_not_as_a_failure():
    """A channel someone deleted by hand first is the end state we wanted."""
    guild, trigger, room, category = _live_hub_guild(category_refuses=NOT_FOUND)
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert outcome.failed == ()
    assert CATEGORY_ID in outcome.deleted
    assert outcome.message == "Removed the **Ranked** hub."


async def test_remove_hub_does_not_count_the_trigger_channel_twice():
    """The trigger is a CHILD of the category and the cache lags the delete.

    ``category.channels`` still lists the channel deleted a moment ago (the
    cache only drops it when the gateway says so), so a second attempt would
    double-count it: one refusal reported as two stuck channels.
    """
    guild, trigger, room, category = _live_hub_guild(trigger_refuses=FORBIDDEN)
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert outcome.failed == (TRIGGER_ID,)  # once, not twice
    assert trigger.deletes == 1
    assert outcome.deleted.count(CATEGORY_ID) == 1


async def test_remove_hub_message_counts_the_channels_that_survived():
    guild, trigger, room, category = _live_hub_guild(
        room_refuses=FORBIDDEN, category_refuses=FORBIDDEN
    )
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert len(outcome.failed) == 2
    assert "2 of its channels" in outcome.message
    # The plural FORM, not just the number: the singular msgid carries {count}
    # too, so "remove them" vs "remove it" is the only thing left that proves
    # ngettext really picked the plural for n=2.
    assert "remove them by hand" in outcome.message


async def test_remove_hub_message_spells_the_count_even_for_a_single_channel():
    """The singular msgid must carry {count}, never a hardcoded "1".

    A catalog is allowed to route EVERY n to msgstr[0] - locales/ja ships
    ``nplurals=2; plural=0;`` and does exactly that - so a singular that spelled
    its own number would answer "1 of its channels" for three refused ones, in
    the single sentence whose entire job is telling the truth about that count.
    """
    guild, trigger, room, category = _live_hub_guild(category_refuses=FORBIDDEN)
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert outcome.failed == (CATEGORY_ID,)
    assert "1 of its channels" in outcome.message
    assert "remove it by hand" in outcome.message


async def test_both_plural_forms_of_the_refusal_msgid_spell_the_count():
    """Pin the msgids themselves: {count} in BOTH forms, whatever n selects.

    The test above can only see the form gettext chose. This one reads the two
    source strings the cog hands to ngettext, so a singular that goes back to a
    literal "1" fails even in a catalog that never selects it.
    """
    import inspect

    source = inspect.getsource(rooms.TemporaryRooms._remove_hub)
    call = source.split("ngettext(", 1)[1].split("len(failed)", 1)[0]
    assert call.count("{count}") == 2, call
    assert "but 1 of its" not in call.replace("\n", " ")


async def test_remove_hub_logs_a_refusal_as_a_warning_with_the_channel_id(caplog):
    """A deletion the user will NOTICE failed is not debug noise."""
    guild, trigger, room, category = _live_hub_guild(category_refuses=FORBIDDEN)
    cog = _cog([_hub()])

    with caplog.at_level(logging.DEBUG, logger="cogs.config.rooms"):
        await cog._remove_hub(guild, "abc12345")

    refusals = [
        record
        for record in caplog.records
        if str(CATEGORY_ID) in record.getMessage() and "delete" in record.getMessage()
    ]
    assert refusals, "the refused deletion was not logged at all"
    assert [record.levelno for record in refusals] == [logging.WARNING]
    assert "100" in refusals[0].getMessage()  # and it names the guild


async def test_remove_hub_keeps_one_traceback_and_one_summary_for_a_full_hub(caplog):
    """A category can hold 50 children: one click must not log 51 tracebacks.

    The scale-first rule applies to the log too. Every refusal is still
    REPORTED - ``failed`` names all of them and the sentence counts them - but
    the file log gets the traceback once (the cause is the same missing
    permission for all of them) plus one aggregate line naming the ids.
    """
    children = [_Voice(700 + i, name="room %d" % i, refuse=FORBIDDEN) for i in range(12)]
    category = _Category(
        CATEGORY_ID, name="RANKED", children=children, refuse=FORBIDDEN
    )
    guild = _Guild(channels=[category, *children])
    for channel in (*children, category):
        channel.guild = guild
    # The trigger channel is not in this guild's channel list, so the teardown
    # is exactly the category and its twelve children: 13 refusals.
    cog = _cog([_hub()])

    with caplog.at_level(logging.DEBUG, logger="cogs.config.rooms"):
        outcome = await cog._remove_hub(guild, "abc12345")

    assert len(outcome.failed) == 13  # every single one is still reported
    assert "13 of its channels" in outcome.message
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    with_traceback = [r for r in warnings if r.exc_info]
    assert len(with_traceback) == 1  # not 13
    summary = [r for r in warnings if not r.exc_info][0].getMessage()
    assert "13" in summary and "100" in summary  # the count and the guild
    assert str(CATEGORY_ID) in summary  # and the ids a human has to go delete


async def test_remove_hub_counts_a_transport_failure_as_still_there(caplog):
    """A timeout is not a discord exception, and must not abort the teardown.

    The channel is exactly as visible to the user as after a 403, so it belongs
    in ``failed`` - and the config drop below it still has to happen.
    """
    guild, trigger, room, category = _live_hub_guild(category_refuses=TRANSPORT_FLAKE)
    cog = _cog([_hub()])

    with caplog.at_level(logging.DEBUG, logger="cogs.config.rooms"):
        outcome = await cog._remove_hub(guild, "abc12345")

    assert outcome.failed == (CATEGORY_ID,)
    assert set(outcome.deleted) == {TRIGGER_ID, ROOM_ID}
    assert cog.saved == [(100, [])]  # the config went anyway
    assert [r.levelno for r in caplog.records if r.exc_info] == [logging.WARNING]


async def test_remove_hub_on_a_hub_that_is_already_gone_touches_nothing():
    guild, trigger, room, category = _live_hub_guild()
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "deadbeef")

    assert outcome.removed is False
    assert outcome.message == "That hub no longer exists."
    assert cog.saved == []
    assert category.deletes == 0


async def test_remove_hub_survives_a_hub_whose_channels_are_all_missing():
    """Nothing to delete is not a failure: no channel id ends up in either list."""
    guild = _Guild(channels=[])
    cog = _cog([_hub()])

    outcome = await cog._remove_hub(guild, "abc12345")

    assert outcome.removed is True
    assert outcome.deleted == ()
    assert outcome.failed == ()
    assert outcome.message == "Removed the **Ranked** hub."


# ---------------------------------------------------------------------------
# _add_hub: two round trips, so a half-created hub must leave NOTHING behind
# ---------------------------------------------------------------------------
async def test_add_hub_rolls_the_category_back_when_the_trigger_fails():
    guild = _Guild(trigger_refuses=SERVER_ERROR)
    cog = _cog()

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=0,
    )

    assert isinstance(outcome, autoroom.HubCreation)
    assert [c.deletes for c in guild.created] == [1]  # the category was undone
    assert outcome.orphan_category_id is None  # nothing left behind
    assert outcome.message == "Something went wrong while creating the hub's channels."
    assert cog.saved == []  # a failed create never writes config


async def test_add_hub_names_the_category_it_could_not_roll_back():
    """The rollback failing is the same honesty rule as a refused delete."""
    guild = _Guild(trigger_refuses=SERVER_ERROR, rollback_refuses=FORBIDDEN)
    cog = _cog()

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=0,
    )

    assert outcome.orphan_category_id == CATEGORY_ID
    assert "by hand" in outcome.message
    assert cog.saved == []


@pytest.mark.parametrize("rollback_refuses, orphan", [(None, None), (FORBIDDEN, CATEGORY_ID)])
async def test_add_hub_rolls_back_a_transport_failure_too(rollback_refuses, orphan):
    """The flake that is NOT a discord exception must not escape the rollback.

    ``create_voice_channel`` goes out over aiohttp: a timeout there is a plain
    ``asyncio.TimeoutError``. Catching only ``discord.HTTPException`` let it
    propagate, which orphaned the category AND told nobody - the very defect
    this rollback exists to close, arriving by another exception class.
    """
    guild = _Guild(
        trigger_refuses=TRANSPORT_FLAKE, rollback_refuses=rollback_refuses
    )
    cog = _cog()

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=0,
    )

    assert isinstance(outcome, autoroom.HubCreation)  # answered, not raised
    assert [c.deletes for c in guild.created] == [1]  # the rollback ran
    assert outcome.orphan_category_id == orphan
    assert cog.saved == []


async def test_add_hub_reports_a_transport_failure_on_the_category_itself():
    guild = _Guild(category_refuses=TRANSPORT_FLAKE)
    cog = _cog()

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=0,
    )

    assert guild.created == []
    assert outcome.orphan_category_id is None
    assert outcome.message == "Something went wrong while creating the hub's channels."


async def test_add_hub_has_nothing_to_roll_back_when_the_category_fails():
    guild = _Guild(category_refuses=FORBIDDEN)
    cog = _cog()

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=0,
    )

    assert guild.created == []  # no category to undo
    assert outcome.orphan_category_id is None
    assert outcome.message == "Something went wrong while creating the hub's channels."


async def test_add_hub_saves_and_reports_the_created_hub():
    guild = _Guild()
    cog = _cog()

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=4,
    )

    assert [c.deletes for c in guild.created] == [0, 0]  # nothing rolled back
    assert outcome.orphan_category_id is None
    assert "Created the **Ranked** hub" in outcome.message
    guild_id, saved = cog.saved[0]
    assert (guild_id, len(saved)) == (100, 1)
    assert saved[0]["hub_channel_id"] == TRIGGER_ID


@pytest.mark.parametrize(
    "hubs, guild_kwargs",
    [
        ([_hub(hub_id="h%d" % i) for i in range(autoroom.MAX_HUBS)], {}),
        ([], {"channels": [_Category(9000 + i) for i in range(autoroom.MAX_CATEGORIES)]}),
    ],
)
async def test_add_hub_refusals_answer_with_the_same_record(hubs, guild_kwargs):
    """Every path returns a HubCreation - a caller reads ``.message`` blindly."""
    guild = _Guild(**guild_kwargs)
    cog = _cog(hubs)

    outcome = await cog._add_hub(
        guild,
        label="Ranked",
        category_name="RANKED",
        hub_name="Join to create",
        template="{user}'s room",
        user_limit=0,
    )

    assert isinstance(outcome, autoroom.HubCreation)
    assert outcome.orphan_category_id is None
    assert guild.created == []  # refused BEFORE anything was created
    assert cog.saved == []


# ---------------------------------------------------------------------------
# The OTHER surface: the Discord panel told the same lie the dashboard did
# ---------------------------------------------------------------------------
class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class _Response:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True


class _Interaction:
    def __init__(self, guild):
        self.guild = guild
        self.response = _Response()
        self.followup = _Followup()


async def test_panel_remove_sends_the_qualified_sentence_not_the_record():
    """The panel must show the message, not str() of the outcome object."""
    guild, trigger, room, category = _live_hub_guild(category_refuses=FORBIDDEN)
    cog = _cog([_hub()])
    interaction = _Interaction(guild)
    rerendered = []
    panel = types.SimpleNamespace(
        cog=cog, _rerender=lambda: _record(rerendered), hubs=[]
    )

    await rooms_config.AutoroomPanel._on_remove(panel, interaction, "abc12345")

    assert len(interaction.followup.sent) == 1
    sent = interaction.followup.sent[0]
    assert isinstance(sent, str)
    assert "could not be deleted" in sent
    assert rerendered == [True]  # the panel still redrew


async def test_panel_add_sends_the_message_of_the_creation_record():
    guild = _Guild(trigger_refuses=SERVER_ERROR, rollback_refuses=FORBIDDEN)
    cog = _cog()
    interaction = _Interaction(guild)
    rerendered = []
    modal = types.SimpleNamespace(
        cog=cog,
        panel=types.SimpleNamespace(_rerender=lambda: _record(rerendered)),
        label_input=types.SimpleNamespace(value="Ranked"),
        category_input=types.SimpleNamespace(value="RANKED"),
        hub_input=types.SimpleNamespace(value="Join to create"),
        template_input=types.SimpleNamespace(value="{user}'s room"),
        limit_select=types.SimpleNamespace(values=["0"]),
    )

    await rooms_config.AddHubModal.on_submit(modal, interaction)

    sent = interaction.followup.sent[0]
    assert isinstance(sent, str)
    assert "by hand" in sent  # the leftover category is named to THIS user too
    assert rerendered == [True]


async def _record(sink):
    sink.append(True)
