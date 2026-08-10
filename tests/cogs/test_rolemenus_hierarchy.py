"""The configurer-hierarchy guard on the role-menu builder's role picker.

The picker is the ONLY way an option enters a menu draft, and the builder is
gated by ``manage_roles`` alone - so without a hierarchy check a moderator could
publish a dropdown handing out any role below Yasuho, including roles above
their own head. Refused roles are dropped from the pick (the good ones survive)
and reported once, ephemerally.

Fakes only: no Discord, no DB.
"""

import types

import discord
import pytest

from cogs.config.rolemenus import RoleMenuBuilder, _RolePicker


class FakeRole:
    def __init__(
        self, role_id=888, position=5, name="Colour", managed=False, default=False
    ):
        self.id = role_id
        self.position = position
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class FakeMember:
    def __init__(self, member_id, top_position, administrator=False):
        self.id = member_id
        self.top_role = FakeRole(1, position=top_position, name="Top")
        self.guild_permissions = types.SimpleNamespace(administrator=administrator)


class FakeGuild:
    def __init__(self, bot_top=10, owner_id=999, roles=None):
        self.id = 100
        self.owner_id = owner_id
        self.me = FakeMember(42, top_position=bot_top)
        self._roles = {r.id: r for r in (roles or [])}

    def get_role(self, role_id):
        # Only the picker's default_values reads this back, and discord.py
        # accepts a bare Object there (a real Role can't be built offline).
        return discord.Object(id=role_id) if role_id in self._roles else None

    def get_channel(self, channel_id):
        return None


@pytest.fixture
def patched_values(monkeypatch):
    """``RoleSelect.values`` is a read-only property fed by the interaction
    payload; point it at what the test picked instead."""
    monkeypatch.setattr(
        discord.ui.RoleSelect,
        "values",
        property(lambda self: getattr(self, "_test_values", [])),
        raising=False,
    )
    yield


def _picker(guild, values, author_id=7):
    """A picker bound to a real builder, with the select's picked values stubbed."""
    builder = RoleMenuBuilder(
        cog=None, guild=guild, author_id=author_id, draft={"options": []}
    )
    builder.message = None
    picker = _RolePicker(builder)
    picker._test_values = values
    return builder, picker


def _interaction(make_interaction, member):
    interaction = make_interaction()
    interaction.user = member
    return interaction


async def test_picker_drops_roles_the_configurer_does_not_outrank(
    make_interaction, patched_values
):
    ok = FakeRole(888, position=4, name="Blue")
    too_high = FakeRole(999, position=9, name="Staff")
    guild = FakeGuild(bot_top=10, roles=[ok, too_high])
    builder, picker = _picker(guild, [ok, too_high])
    interaction = _interaction(make_interaction, FakeMember(7, top_position=5))

    await picker.callback(interaction)

    # Only the role the mod outranks survived into the draft.
    assert [o["role_id"] for o in builder.draft["options"]] == [888]
    # ... and they were told which one was left out, ephemerally, unpinged.
    assert len(interaction.followups) == 1
    args, kwargs = interaction.followups[0]
    assert "<@&999>" in args[0]
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].roles is False


async def test_picker_drops_a_role_above_the_bot(make_interaction, patched_values):
    above_bot = FakeRole(999, position=11, name="Admin")
    guild = FakeGuild(bot_top=10, roles=[above_bot])
    builder, picker = _picker(guild, [above_bot], author_id=7)
    # The guild owner clears the invoker half of the check; the bot ceiling
    # still refuses (a menu on that role would 403 on every pick).
    guild.owner_id = 7
    interaction = _interaction(make_interaction, FakeMember(7, top_position=1))

    await picker.callback(interaction)

    assert builder.draft["options"] == []
    assert len(interaction.followups) == 1


async def test_picker_drops_a_role_yasuho_cannot_grant(
    make_interaction, patched_values
):
    """Position is not the whole question: @everyone and an integration-owned
    role sit below both parties and can still never be handed out, so an option
    on one would 403 on every pick with the member seeing nothing happen."""
    ok = FakeRole(888, position=4, name="Blue")
    booster = FakeRole(777, position=3, name="Booster", managed=True)
    everyone = FakeRole(100, position=0, name="@everyone", default=True)
    guild = FakeGuild(bot_top=10, roles=[ok, booster, everyone])
    builder, picker = _picker(guild, [ok, booster, everyone])
    interaction = _interaction(make_interaction, FakeMember(7, top_position=9))

    await picker.callback(interaction)

    assert [o["role_id"] for o in builder.draft["options"]] == [888]
    assert len(interaction.followups) == 1
    body = interaction.followups[0][0][0]
    assert "<@&777>" in body and "<@&100>" in body


async def test_picker_reports_refusals_on_the_response_when_the_edit_fell_back(
    make_interaction, patched_values
):
    """refresh_layout falls back to editing the stored message when the live
    edit fails, which leaves the interaction UNANSWERED - a followup would then
    404 and the member would never learn a role was dropped."""
    ok = FakeRole(888, position=4, name="Blue")
    too_high = FakeRole(999, position=9, name="Staff")
    guild = FakeGuild(bot_top=10, roles=[ok, too_high])
    builder, picker = _picker(guild, [ok, too_high])
    interaction = _interaction(make_interaction, FakeMember(7, top_position=5))

    async def _failing_edit(*_args, **_kwargs):
        raise discord.HTTPException(
            types.SimpleNamespace(status=500, reason="server error"), "nope"
        )

    interaction.response.edit_message = _failing_edit

    await picker.callback(interaction)

    # The pick still landed, and the notice went to the still-open response.
    assert [o["role_id"] for o in builder.draft["options"]] == [888]
    assert interaction.followups == []
    assert len(interaction.sent) == 1
    assert "<@&999>" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_picker_keeps_every_allowed_role_and_says_nothing(
    make_interaction, patched_values
):
    """Counter-test: an all-allowed pick behaves exactly as before."""
    blue = FakeRole(888, position=3, name="Blue")
    red = FakeRole(777, position=4, name="Red")
    guild = FakeGuild(bot_top=10, roles=[blue, red])
    builder, picker = _picker(guild, [blue, red])
    interaction = _interaction(make_interaction, FakeMember(7, top_position=9))

    await picker.callback(interaction)

    assert [o["role_id"] for o in builder.draft["options"]] == [888, 777]
    assert [o["label"] for o in builder.draft["options"]] == ["Blue", "Red"]
    assert interaction.followups == []
    assert interaction.edits  # the panel refreshed in place
    assert interaction.sent == []  # ... and the error path never ran
