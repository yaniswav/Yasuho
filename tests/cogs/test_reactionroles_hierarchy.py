"""The configurer-hierarchy guard on the reaction-role add paths.

``manage_roles`` opens the command and the guided modal, but it proves NOTHING
about the invoker's own position: without a hierarchy check a plain moderator
could publish an emoji that hands out any role BELOW YASUHO - including roles
above their own head - which is a self-promotion primitive, not a config option.
Both add paths (the classic ``reactionrole add`` and ``AddReactionRoleModal``)
must therefore refuse before anything is persisted or reacted.

Fakes only: no Discord, no DB. The pool is asserted to have received NOTHING on
a refusal, which is the property that matters (a refused mapping must not exist).
"""

import types

import pytest

from cogs.config.reactionroles import AddReactionRoleModal, ReactionRoles


class FakeRole:
    """Role ordering is by position; ``>=`` is what the hierarchy half compares,
    while ``managed`` / ``is_default()`` are what the assignability half reads."""

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
    """Guild with an owner, Yasuho's member object and a channel lookup."""

    def __init__(self, bot_top=10, owner_id=999):
        self.id = 100
        self.owner_id = owner_id
        self.me = FakeMember(42, top_position=bot_top)
        self.channel = FakeChannel()

    def get_channel_or_thread(self, channel_id):
        return self.channel


class FakeMessage:
    def __init__(self):
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class FakeChannel:
    def __init__(self, channel_id=555):
        self.id = channel_id
        self.message = FakeMessage()

    async def fetch_message(self, mid):
        return self.message


def _cog(fake_pool):
    return ReactionRoles(types.SimpleNamespace(db_pool=fake_pool))


def _typing_ctx():
    class _Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    return _Typing()


class FakeContext:
    """Enough commands.Context for reactionrole_add (author/guild/typing/send)."""

    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.channel = guild.channel
        self.interaction = None
        self.sends = []

    def typing(self):
        return _typing_ctx()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


# ---------------------------------------------------------------------------
# Classic command path
# ---------------------------------------------------------------------------
async def test_add_refuses_a_role_at_or_above_the_configurer(fake_pool):
    """A mod whose top role is not strictly above the role gets nothing done."""
    guild = FakeGuild(bot_top=10)
    mod = FakeMember(7, top_position=5)  # same height as the role
    ctx = FakeContext(mod, guild)
    cog = _cog(fake_pool)

    await cog.reactionrole_add(cog, ctx, "777", "\U0001F3AE", FakeRole(888, position=5))

    assert fake_pool.calls == []  # nothing persisted
    assert guild.channel.message.reactions == []  # nothing reacted
    assert "equal to or above your highest role" in ctx.sends[0][0][0]


async def test_add_refuses_a_role_above_the_bot(fake_pool):
    guild = FakeGuild(bot_top=10)
    owner = FakeMember(999, top_position=50)  # the owner: outranks everyone
    ctx = FakeContext(owner, guild)
    cog = _cog(fake_pool)

    await cog.reactionrole_add(cog, ctx, "777", "\U0001F3AE", FakeRole(888, position=11))

    assert fake_pool.calls == []
    assert "isn't above that role" in ctx.sends[0][0][0]


async def test_add_refuses_a_managed_role_the_configurer_outranks(fake_pool):
    """Position is not the whole question: an integration-owned role can sit
    below both parties and still be ungrantable, so the mapping would 403 on
    every reaction with nobody ever told."""
    guild = FakeGuild(bot_top=10)
    mod = FakeMember(7, top_position=9)
    ctx = FakeContext(mod, guild)
    cog = _cog(fake_pool)

    await cog.reactionrole_add(
        cog, ctx, "777", "\U0001F3AE", FakeRole(888, position=5, managed=True)
    )

    assert fake_pool.calls == []
    assert guild.channel.message.reactions == []
    assert "managed by an" in ctx.sends[0][0][0]


async def test_add_refuses_the_everyone_role(fake_pool):
    guild = FakeGuild(bot_top=10)
    mod = FakeMember(7, top_position=9)
    ctx = FakeContext(mod, guild)
    cog = _cog(fake_pool)

    await cog.reactionrole_add(
        cog, ctx, "777", "\U0001F3AE", FakeRole(100, position=0, default=True)
    )

    assert fake_pool.calls == []
    assert guild.channel.message.reactions == []
    assert "managed by an" in ctx.sends[0][0][0]


async def test_add_allows_a_role_below_both(fake_pool):
    """Counter-test: the normal case still persists + reacts."""
    guild = FakeGuild(bot_top=10)
    mod = FakeMember(7, top_position=9)
    ctx = FakeContext(mod, guild)
    cog = _cog(fake_pool)

    await cog.reactionrole_add(cog, ctx, "777", "\U0001F3AE", FakeRole(888, position=5))

    assert len(fake_pool.calls) == 1
    method, query, args = fake_pool.calls[0]
    assert method == "execute" and "INSERT INTO reaction_roles" in query
    assert args == (777, "\U0001F3AE", 888, 100)
    assert cog.cache[(777, "\U0001F3AE")] == 888
    assert guild.channel.message.reactions == ["\U0001F3AE"]


async def test_add_allows_the_guild_owner_to_pick_any_role_under_the_bot(fake_pool):
    """The owner bypasses the invoker half of the check (like addrole does)."""
    guild = FakeGuild(bot_top=10, owner_id=7)
    owner = FakeMember(7, top_position=1)
    ctx = FakeContext(owner, guild)
    cog = _cog(fake_pool)

    await cog.reactionrole_add(cog, ctx, "777", "\U0001F3AE", FakeRole(888, position=9))

    assert len(fake_pool.calls) == 1
    assert cog.cache[(777, "\U0001F3AE")] == 888


# ---------------------------------------------------------------------------
# Guided modal path (the one a slash user actually reaches)
# ---------------------------------------------------------------------------
def _modal(cog, guild, role, ref="777", emoji="\U0001F3AE"):
    modal = AddReactionRoleModal(cog, guild, default_channel_id=555)
    modal.role_select._test_values = [role]
    modal.ref_field._test_value = ref
    modal.emoji_field._test_value = emoji
    return modal


@pytest.fixture
def patched_inputs(monkeypatch):
    """``TextInput.value`` / ``RoleSelect.values`` are read-only properties fed
    by the submit payload; point them at what the test set instead."""
    import discord

    monkeypatch.setattr(
        discord.ui.TextInput,
        "value",
        property(lambda self: self._test_value),
        raising=False,
    )
    monkeypatch.setattr(
        discord.ui.RoleSelect,
        "values",
        property(lambda self: self._test_values),
        raising=False,
    )
    yield


async def test_modal_refuses_a_role_at_or_above_the_configurer(
    fake_pool, make_interaction, patched_inputs
):
    guild = FakeGuild(bot_top=10)
    cog = _cog(fake_pool)
    modal = _modal(cog, guild, FakeRole(888, position=5))
    interaction = make_interaction()
    interaction.user = FakeMember(7, top_position=5)

    await modal.on_submit(interaction)

    assert fake_pool.calls == []
    assert guild.channel.message.reactions == []
    assert cog.cache == {}
    assert "equal to or above your highest role" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_modal_refuses_a_managed_role_the_configurer_outranks(
    fake_pool, make_interaction, patched_inputs
):
    guild = FakeGuild(bot_top=10)
    cog = _cog(fake_pool)
    modal = _modal(cog, guild, FakeRole(888, position=4, managed=True))
    interaction = make_interaction()
    interaction.user = FakeMember(7, top_position=9)

    await modal.on_submit(interaction)

    assert fake_pool.calls == []
    assert guild.channel.message.reactions == []
    assert cog.cache == {}
    assert "managed by an" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_modal_allows_a_role_below_both(
    fake_pool, make_interaction, patched_inputs
):
    guild = FakeGuild(bot_top=10)
    cog = _cog(fake_pool)
    modal = _modal(cog, guild, FakeRole(888, position=4))
    interaction = make_interaction()
    interaction.user = FakeMember(7, top_position=9)

    await modal.on_submit(interaction)

    assert len(fake_pool.calls) == 1
    assert cog.cache[(777, "\U0001F3AE")] == 888
    assert interaction.followups  # the confirmation embed went out
