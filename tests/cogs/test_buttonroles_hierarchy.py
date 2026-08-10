"""The configurer-hierarchy guard on the button-role builder.

``BuilderView._can_assign`` only ever asked whether YASUHO could hand a role
out; nothing asked whether the person building the panel outranks it. Since the
builder is gated by ``manage_roles`` alone, that let any moderator publish a
button granting a role above their own head (anything under the bot's ceiling)
and click it themselves. ``on_add_role`` must refuse before the customise modal
is ever opened, so nothing reaches the draft.

Fakes only: no Discord, no DB.
"""

import types

from cogs.config.buttonroles import BuilderView


class FakeRole:
    def __init__(self, role_id=888, position=5, name="Colour", guild=None, managed=False):
        self.id = role_id
        self.position = position
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.guild = guild
        self.managed = managed

    def is_default(self):
        return self.id == 100  # @everyone shares the guild id

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
    def __init__(self, bot_top=10, owner_id=999):
        self.id = 100
        self.owner_id = owner_id
        self.me = FakeMember(42, top_position=bot_top)

    def get_role(self, role_id):
        return None

    def get_channel(self, channel_id):
        return None


def _builder(guild, author_id=7):
    return BuilderView(
        cog=None,
        guild=guild,
        author_id=author_id,
        target_channel_id=None,
        config={"embed": {}, "buttons": []},
    )


def _interaction(make_interaction, member):
    """A FakeInteraction whose user is a Member and whose response records the
    modal the happy path opens (the conftest fake has no send_modal)."""
    interaction = make_interaction()
    interaction.user = member
    interaction.modals = []

    async def send_modal(modal):
        interaction.modals.append(modal)
        interaction.response._done = True

    interaction.response.send_modal = send_modal
    return interaction


async def test_add_role_refuses_a_role_at_or_above_the_configurer(make_interaction):
    guild = FakeGuild(bot_top=10)
    builder = _builder(guild)
    role = FakeRole(888, position=5, guild=guild)
    interaction = _interaction(make_interaction, FakeMember(7, top_position=5))

    await builder.on_add_role(interaction, role)

    assert builder.config["buttons"] == []  # nothing entered the draft
    assert interaction.modals == []  # the customise modal never opened
    assert "equal to or above your highest role" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_add_role_refuses_a_role_above_the_bot(make_interaction):
    guild = FakeGuild(bot_top=10, owner_id=7)
    builder = _builder(guild)
    role = FakeRole(888, position=11, guild=guild)
    interaction = _interaction(make_interaction, FakeMember(7, top_position=50))

    await builder.on_add_role(interaction, role)

    assert interaction.modals == []
    assert "isn't above that role" in interaction.sent[0][0][0]


async def test_add_role_allows_a_role_below_both(make_interaction):
    """Counter-test: the customise modal still opens for a normal role."""
    guild = FakeGuild(bot_top=10)
    builder = _builder(guild)
    role = FakeRole(888, position=4, guild=guild)
    interaction = _interaction(make_interaction, FakeMember(7, top_position=9))

    await builder.on_add_role(interaction, role)

    assert interaction.sent == []
    assert len(interaction.modals) == 1
    assert interaction.modals[0].role is role


async def test_add_role_still_refuses_a_managed_role_the_configurer_outranks(
    make_interaction,
):
    """The pre-existing bot-side guard survives the new invoker-side one."""
    guild = FakeGuild(bot_top=10)
    builder = _builder(guild)
    role = FakeRole(888, position=4, guild=guild, managed=True)
    interaction = _interaction(make_interaction, FakeMember(7, top_position=9))

    await builder.on_add_role(interaction, role)

    assert interaction.modals == []
    assert "managed by an" in interaction.sent[0][0][0]
