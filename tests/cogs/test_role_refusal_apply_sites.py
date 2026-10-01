"""Defence in depth at two more apply sites: a dangerous role, however it got
configured, must never be handed out by the verify button or the Twitch
go-live alert. Everywhere except ``?mute`` this is a SILENT skip (log only,
see tools.role_audit) - there is no moderator waiting on these.
"""

import types

import discord
import pytest

import cogs.config.twitch as twitch_module
import cogs.config.verification as verification_module
from tools import settings


class _FakeRole:
    def __init__(self, role_id=445566778899001122, name="Role", permissions=None):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.managed = False
        self.permissions = permissions or discord.Permissions.none()

    def __lt__(self, other):
        return self.id < other.id

    def __ge__(self, other):
        return self.id >= other.id


class _FakeGuild:
    def __init__(self, *, channels=(), roles=(), top_role_id=10**19):
        self.id = 42
        self.name = "guild"
        self._channels = {c.id: c for c in channels}
        self._roles = {r.id: r for r in roles}
        self.roles = list(roles)
        self.me = types.SimpleNamespace(top_role=_FakeRole(top_role_id, "bot"))
        self.preferred_locale = "en-US"

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)


class _FakeChannel:
    def __init__(self, channel_id=877293049194057728):
        self.id = channel_id
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


class _FakeMember:
    def __init__(self, guild, *, roles=(), member_id=99):
        self.id = member_id
        self.guild = guild
        self.bot = False
        self.display_name = "Yanis"
        self.mention = f"<@{member_id}>"
        self.roles = list(roles)
        self.display_avatar = types.SimpleNamespace(url="https://cdn/av.png")
        self.added = []
        self.removed = []

    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles)
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self.removed.extend(roles)


def _stub_settings(monkeypatch, blob):
    async def _get_guild(_pool, _guild_id, key, default=None):
        return blob.get(key, default)

    async def _get_user(_pool, _user_id, _key, default=None):
        return default

    monkeypatch.setattr(settings, "get_guild", _get_guild)
    monkeypatch.setattr(settings, "get_user", _get_user)


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_button_refuses_a_dangerous_role(monkeypatch, make_interaction):
    role = _FakeRole(permissions=discord.Permissions(manage_roles=True))
    guild = _FakeGuild(roles=[role])
    member = _FakeMember(guild)
    _stub_settings(monkeypatch, {"verify_role": role.id})

    interaction = make_interaction(guild_id=guild.id)
    interaction.client = types.SimpleNamespace(db_pool=None)
    interaction.guild = guild
    interaction.user = member
    monkeypatch.setattr(verification_module.discord, "Member", _FakeMember)

    await verification_module.VerifyButton().callback(interaction)

    assert member.added == []
    assert interaction.sent
    args, kwargs = interaction.sent[-1]
    text = (args[0] if args else kwargs.get("content", ""))
    assert "carries permissions" in text


@pytest.mark.asyncio
async def test_verify_button_still_grants_a_harmless_role(monkeypatch, make_interaction):
    role = _FakeRole(permissions=discord.Permissions.none())
    guild = _FakeGuild(roles=[role])
    member = _FakeMember(guild)
    _stub_settings(monkeypatch, {"verify_role": role.id})

    interaction = make_interaction(guild_id=guild.id)
    interaction.client = types.SimpleNamespace(db_pool=None)
    interaction.guild = guild
    interaction.user = member
    monkeypatch.setattr(verification_module.discord, "Member", _FakeMember)

    await verification_module.VerifyButton().callback(interaction)

    assert member.added == [role]


# ---------------------------------------------------------------------------
# twitch live role
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_twitch_go_live_refuses_a_dangerous_role(monkeypatch, fake_pool):
    channel = _FakeChannel()
    role = _FakeRole(name="Live", permissions=discord.Permissions(kick_members=True))
    guild = _FakeGuild(channels=[channel], roles=[role])
    member = _FakeMember(guild)
    _stub_settings(
        monkeypatch,
        {"twitch": {"enabled": True, "channel_id": channel.id, "role_id": role.id}},
    )
    fake_pool.fetchrow_return = {"channel_id": 0}
    cog = twitch_module.Twitch(types.SimpleNamespace(db_pool=fake_pool))
    activity = types.SimpleNamespace(
        url="https://twitch.tv/x", game="Ranked", name="live!", platform="Twitch"
    )

    await cog._on_go_live(member, activity)

    assert member.added == []
    # The alert itself (unrelated to the role) still posts.
    assert len(channel.sends) == 1


@pytest.mark.asyncio
async def test_twitch_go_live_still_assigns_a_harmless_role(monkeypatch, fake_pool):
    channel = _FakeChannel()
    role = _FakeRole(name="Live", permissions=discord.Permissions.none())
    guild = _FakeGuild(channels=[channel], roles=[role])
    member = _FakeMember(guild)
    _stub_settings(
        monkeypatch,
        {"twitch": {"enabled": True, "channel_id": channel.id, "role_id": role.id}},
    )
    fake_pool.fetchrow_return = {"channel_id": 0}
    cog = twitch_module.Twitch(types.SimpleNamespace(db_pool=fake_pool))
    activity = types.SimpleNamespace(
        url="https://twitch.tv/x", game="Ranked", name="live!", platform="Twitch"
    )

    await cog._on_go_live(member, activity)

    assert member.added == [role]
