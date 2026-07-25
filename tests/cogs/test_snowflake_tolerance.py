"""The dashboard writes snowflakes as STRINGS: every id read out of a JSONB
config blob must behave exactly as the int spelling does.

The dashboard is a separate Node process, and JS cannot hold a snowflake in a
Number, so it serialises ids as strings. Left uncoerced, ``guild.get_channel
("123")`` / ``guild.get_role("123")`` return None and the feature goes SILENT -
no exception, no log, just nothing happening. Each test below runs the same
scenario twice (int id / string id) and asserts an identical outcome.

Covers the four surfaces that read a Discord id out of guild_settings: welcome
(channel_id), verification (verify_role), twitch (channel_id + role_id) and
automod (the exempt role/channel lists), plus the role-menu JSONB config.
"""

import types

import pytest

import cogs.config.twitch as twitch_module
import cogs.config.verification as verification_module
import cogs.config.welcome as welcome_module
import cogs.moderation.automod as automod_module
from tools import role_menus, settings

CHANNEL_ID = 877293049194057728
ROLE_ID = 445566778899001122

# Each case is (label, stored_value) - the SAME id in both spellings.
ID_SPELLINGS = [
    pytest.param(CHANNEL_ID, id="int"),
    pytest.param(str(CHANNEL_ID), id="string"),
]
ROLE_SPELLINGS = [
    pytest.param(ROLE_ID, id="int"),
    pytest.param(str(ROLE_ID), id="string"),
]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeChannel:
    def __init__(self, channel_id=CHANNEL_ID):
        self.id = channel_id
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


class _FakeRole:
    def __init__(self, role_id=ROLE_ID, name="Verified"):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.managed = False

    def __lt__(self, other):
        return self.id < other.id

    def __ge__(self, other):
        return self.id >= other.id


class _FakeGuild:
    def __init__(self, *, channels=(), roles=(), top_role_id=10**19):
        self.id = 42
        self.name = "guild"
        self.member_count = 7
        self._channels = {c.id: c for c in channels}
        self._roles = {r.id: r for r in roles}
        self.roles = list(roles)
        self.me = types.SimpleNamespace(top_role=_FakeRole(top_role_id, "bot"))
        self.preferred_locale = "en-US"

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)


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
    """Serve ``blob`` for every guild-settings read, bypassing the DB + cache."""

    async def _get_guild(_pool, _guild_id, key, default=None):
        return blob.get(key, default)

    async def _get_user(_pool, _user_id, _key, default=None):
        return default

    monkeypatch.setattr(settings, "get_guild", _get_guild)
    monkeypatch.setattr(settings, "get_user", _get_user)


# ---------------------------------------------------------------------------
# welcome: guild_settings['welcome'].channel_id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ID_SPELLINGS)
async def test_welcome_greets_with_either_id_spelling(monkeypatch, stored):
    channel = _FakeChannel()
    guild = _FakeGuild(channels=[channel])
    member = _FakeMember(guild)
    _stub_settings(
        monkeypatch,
        {"welcome": {"enabled": True, "channel_id": stored, "card": False}},
    )
    cog = welcome_module.Welcome(
        types.SimpleNamespace(db_pool=None, blacklist=set())
    )

    await cog.on_member_join(member)

    assert len(channel.sends) == 1, "the greeting must be posted for both spellings"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ID_SPELLINGS)
async def test_welcome_config_exposes_an_int_channel_id(monkeypatch, stored):
    _stub_settings(monkeypatch, {"welcome": {"enabled": True, "channel_id": stored}})
    cog = welcome_module.Welcome(types.SimpleNamespace(db_pool=None))

    config = await cog.get_config(1)

    assert config["channel_id"] == CHANNEL_ID


# ---------------------------------------------------------------------------
# verification: guild_settings['verify_role']
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ROLE_SPELLINGS)
async def test_verify_button_grants_with_either_id_spelling(
    monkeypatch, make_interaction, stored
):
    role = _FakeRole()
    guild = _FakeGuild(roles=[role])
    member = _FakeMember(guild)
    _stub_settings(monkeypatch, {"verify_role": stored})

    interaction = make_interaction(guild_id=guild.id)
    interaction.client = types.SimpleNamespace(db_pool=None)
    interaction.guild = guild
    interaction.user = member
    monkeypatch.setattr(verification_module.discord, "Member", _FakeMember)

    await verification_module.VerifyButton().callback(interaction)

    assert member.added == [role], "the verify role must be granted for both spellings"


# ---------------------------------------------------------------------------
# twitch: guild_settings['twitch'].channel_id / .role_id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("stored_channel", ID_SPELLINGS)
@pytest.mark.parametrize("stored_role", ROLE_SPELLINGS)
async def test_twitch_go_live_posts_and_assigns_with_either_spelling(
    monkeypatch, fake_pool, stored_channel, stored_role
):
    channel = _FakeChannel()
    role = _FakeRole(name="Live")
    guild = _FakeGuild(channels=[channel], roles=[role])
    member = _FakeMember(guild)
    _stub_settings(
        monkeypatch,
        {
            "twitch": {
                "enabled": True,
                "channel_id": stored_channel,
                "role_id": stored_role,
            }
        },
    )
    # The watchlist row carries no per-member override (BIGINT column, 0 = none),
    # so the guild channel from the JSONB blob is what must resolve.
    fake_pool.fetchrow_return = {"channel_id": 0}
    cog = twitch_module.Twitch(types.SimpleNamespace(db_pool=fake_pool))
    activity = types.SimpleNamespace(
        url="https://twitch.tv/x", game="Ranked", name="live!", platform="Twitch"
    )

    await cog._on_go_live(member, activity)

    assert len(channel.sends) == 1, "the go-live alert must post for both spellings"
    assert member.added == [role], "the Live role must land for both spellings"


# ---------------------------------------------------------------------------
# automod: guild_settings['automod_exempt_roles' / 'automod_exempt_channels']
# ---------------------------------------------------------------------------
def _automod_message(guild, member, channel):
    return types.SimpleNamespace(
        guild=guild, author=member, channel=channel, content="hi"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ID_SPELLINGS)
async def test_automod_channel_exemption_with_either_spelling(monkeypatch, stored):
    channel = _FakeChannel()
    guild = _FakeGuild(channels=[channel])
    member = _FakeMember(guild)
    _stub_settings(monkeypatch, {"automod_exempt_channels": [stored]})
    cog = automod_module.AutoMod(types.SimpleNamespace(db_pool=None))

    assert await cog._is_exempt(_automod_message(guild, member, channel)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ROLE_SPELLINGS)
async def test_automod_role_exemption_with_either_spelling(monkeypatch, stored):
    channel = _FakeChannel()
    role = _FakeRole()
    guild = _FakeGuild(channels=[channel], roles=[role])
    member = _FakeMember(guild, roles=[role])
    _stub_settings(monkeypatch, {"automod_exempt_roles": [stored]})
    cog = automod_module.AutoMod(types.SimpleNamespace(db_pool=None))

    assert await cog._is_exempt(_automod_message(guild, member, channel)) is True


@pytest.mark.asyncio
async def test_automod_panel_state_normalises_exempt_ids(monkeypatch, fake_pool):
    guild = _FakeGuild()
    _stub_settings(
        monkeypatch,
        {
            "automod_exempt_roles": [str(ROLE_ID), True, "junk"],
            "automod_exempt_channels": [str(CHANNEL_ID)],
        },
    )
    cog = automod_module.AutoMod(types.SimpleNamespace(db_pool=fake_pool))

    async def _native_state(_guild):
        return {"kw": False, "nspam": False, "nmention": False}

    monkeypatch.setattr(cog, "native_state", _native_state)

    state = await cog._panel_state(guild)

    assert state["exempt_roles"] == [ROLE_ID], "bools and junk must not survive"
    assert state["exempt_channels"] == [CHANNEL_ID]


# ---------------------------------------------------------------------------
# role menus: the role_menus.config JSONB blob
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stored", ROLE_SPELLINGS)
def test_role_menu_options_accept_either_id_spelling(stored):
    out = role_menus.normalize_options([{"role_id": stored, "label": "Red"}])

    assert [o["role_id"] for o in out] == [ROLE_ID]


def test_role_menu_options_still_refuse_bools_and_junk():
    out = role_menus.normalize_options(
        [{"role_id": True}, {"role_id": "x"}, {"role_id": "0"}, {"role_id": -1}]
    )

    assert out == []
