"""A muted member must not be able to keep talking in a thread.

The "Muted" role was provisioned denying ``send_messages`` and nothing else about
threads. On Discord those are different permissions: ``send_messages`` governs
the channel body, ``send_messages_in_threads`` governs posting inside a thread,
and ``create_public_threads`` / ``create_private_threads`` govern opening one. So
the mute silenced the channel and the member simply opened a thread under it and
carried on. Forum channels were worse than partial: a forum post IS a thread, and
``guild.text_channels`` does not list forums at all, so the old three loops
(text / voice / categories) never applied a single overwrite to one.

These tests pin the deny SHAPE per channel type and the fact that
``_ensure_mute_role`` visits every channel type that can carry speech. No
Discord, no DB: the channels are subclasses of the real discord.py classes (so
the ``isinstance`` dispatch under test is the real one) with a recording
``set_permissions``, and the pool is the in-memory fake.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import types

import discord

from cogs.moderation import moderation, mute_perms

THREAD_PERMS = (
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
)


# ---------------------------------------------------------------------------
# Fakes: real discord channel types (for isinstance) + a recording set_permissions
# ---------------------------------------------------------------------------
class _Recording:
    def __init__(self, name, existing=None):
        self.name = name
        self.id = abs(hash(name)) % 100000
        self.applied = []
        # What the channel already has for the mute role. Everything but a fresh
        # guild has something here, and that is the case the re-apply path lives
        # or dies on.
        self._existing = existing or discord.PermissionOverwrite()

    def overwrites_for(self, _role):
        return self._existing

    async def set_permissions(self, role, *, overwrite=None, **_kwargs):
        self.applied.append((role, overwrite))
        self._existing = overwrite


class _Text(_Recording, discord.TextChannel):
    pass


class _Voice(_Recording, discord.VoiceChannel):
    pass


class _Stage(_Recording, discord.StageChannel):
    pass


class _Category(_Recording, discord.CategoryChannel):
    pass


class _Forum(_Recording, discord.ForumChannel):
    pass


class _Role:
    def __init__(self, role_id=77):
        self.id = role_id


class _Guild:
    def __init__(self, channels):
        self.id = 42
        self.channels = channels
        self.created = []
        # Present so a reverted implementation walking the old three lists still
        # runs: the point of the forum assertion is that those lists MISS it.
        self.text_channels = [c for c in channels if isinstance(c, _Text)]
        self.voice_channels = [c for c in channels if isinstance(c, _Voice)]
        self.categories = [c for c in channels if isinstance(c, _Category)]

    async def create_role(self, *, name, permissions):
        self.created.append((name, permissions))
        return _Role()


def _bot(pool):
    return types.SimpleNamespace(
        db_pool=pool, muteroles={}, eager_cache_lock=asyncio.Lock()
    )


def _denied(overwrite):
    """The set of permission names this overwrite explicitly denies."""
    return {name for name, value in overwrite if value is False}


# ---------------------------------------------------------------------------
# The deny shape, per channel type
# ---------------------------------------------------------------------------
def test_a_text_channel_denies_the_thread_permissions():
    denied = _denied(mute_perms.overwrite_for(_Text("general")))

    assert "send_messages" in denied  # the original deny, unchanged
    assert set(THREAD_PERMS) <= denied


def test_a_forum_channel_denies_the_thread_permissions():
    # A forum has no channel body: posting there is creating a thread, so
    # send_messages alone would have left it completely unmuted.
    denied = _denied(mute_perms.overwrite_for(_Forum("help")))

    assert set(THREAD_PERMS) <= denied


def test_a_category_denies_both_halves():
    # Children synchronised to the category inherit this one, so it has to carry
    # the thread bits AND speak.
    denied = _denied(mute_perms.overwrite_for(_Category("Text")))

    assert set(THREAD_PERMS) <= denied
    assert "speak" in denied


def test_a_voice_channel_denies_its_text_chat_too():
    """A voice channel is two rooms wearing one name.

    Denying ``speak`` alone left the muted member typing in the text chat
    attached to the very channel they had just been silenced in - a mute anybody
    walks around by switching from their microphone to their keyboard.
    """
    denied = _denied(mute_perms.overwrite_for(_Voice("General")))

    assert "speak" in denied
    assert "send_messages" in denied
    assert "add_reactions" in denied
    # A voice channel has no threads, so the thread bits are not asserted there.
    assert not set(THREAD_PERMS) & denied


def test_a_stage_channel_gets_the_voice_shape():
    denied = _denied(mute_perms.overwrite_for(_Stage("Stage")))

    assert "speak" in denied
    assert "send_messages" in denied


def test_an_unknown_channel_type_takes_no_overwrite():
    assert mute_perms.overwrite_for(object()) is None


def test_the_role_bitfield_is_still_all_deny():
    """Byte identity: naming the thread bits cannot change a value of 0.

    Role permissions are OR-ed together by Discord, so a role can only GRANT.
    The bitfield is documentation; the overwrites are the mute.
    """
    assert mute_perms.role_permissions().value == 0


# ---------------------------------------------------------------------------
# ... and the creation path applies it everywhere
# ---------------------------------------------------------------------------
async def test_ensure_mute_role_denies_threads_in_every_talking_channel(fake_pool):
    text = _Text("general")
    forum = _Forum("help")
    category = _Category("Text")
    voice = _Voice("General")
    stage = _Stage("Stage")
    guild = _Guild([text, forum, category, voice, stage])
    cog = moderation.Moderation(_bot(fake_pool))

    role = await cog._ensure_mute_role(guild)

    for channel in (text, forum, category):
        assert channel.applied, f"{channel.name} got no overwrite at all"
        applied_role, overwrite = channel.applied[-1]
        assert applied_role is role
        assert set(THREAD_PERMS) <= _denied(overwrite), (
            f"{channel.name} can still be talked in through a thread"
        )

    for channel in (voice, stage):
        denied = _denied(channel.applied[-1][1])
        assert "speak" in denied
        assert "send_messages" in denied, (
            f"{channel.name}'s built-in text chat is still open"
        )


async def test_ensure_mute_role_visits_a_forum_channel(fake_pool):
    """The regression proper: forums are in none of text/voice/categories."""
    forum = _Forum("help")
    guild = _Guild([forum])
    cog = moderation.Moderation(_bot(fake_pool))

    await cog._ensure_mute_role(guild)

    assert forum.applied, "a forum is pure threads and was never muted"


async def test_ensure_mute_role_creates_an_all_deny_role_and_caches_it(fake_pool):
    guild = _Guild([])
    bot = _bot(fake_pool)
    cog = moderation.Moderation(bot)

    role = await cog._ensure_mute_role(guild)

    name, permissions = guild.created[-1]
    assert name == "Muted"
    assert permissions.value == 0
    # Persisted once and primed in the eager cache, as before.
    assert any("INTO muterole" in call[1] for call in fake_pool.calls)
    assert bot.muteroles == {42: role.id}


# ---------------------------------------------------------------------------
# B1: the fix has to REACH a guild that already has a Muted role
# ---------------------------------------------------------------------------
# The deny shape was only ever written when the role was CREATED, and every
# guild running this bot created its Muted role long ago - so a correction to
# that shape reached, in production, exactly nobody. Worse, the new-channel
# listener carried its own inline copy of the OLD shape, so even a freshly
# provisioned guild re-opened the hole in every channel created afterwards.
# These pin both legs.


class _RoleGuild(_Guild):
    """A guild that already HAS the mute role (the only kind that exists live)."""

    def __init__(self, channels, role):
        super().__init__(channels)
        self._role = role

    def get_role(self, role_id):
        return self._role if role_id == self._role.id else None


def _sync_cog(pool, role_id=77):
    cog = moderation.Moderation(_bot(pool))

    async def _get_mute_role_id(_guild_id):
        return role_id

    cog._get_mute_role_id = _get_mute_role_id
    return cog


class _Ctx:
    def __init__(self, guild):
        self.guild = guild
        self.interaction = None
        self.sent = []

    async def send(self, content=None, **_kwargs):
        self.sent.append(content)


async def test_a_guild_that_already_has_the_role_can_take_the_fix(fake_pool):
    """The regression: without a re-apply path the thread fix ships INERT.

    ``_ensure_mute_role`` is called from exactly one place and only when the
    guild has no mute role id at all, so a server that muted somebody once, ever,
    keeps its thread-permissive overwrites for good.
    """
    role = _Role()
    text = _Text("general")
    forum = _Forum("help")
    guild = _RoleGuild([text, forum], role)
    cog = _sync_cog(fake_pool, role.id)
    ctx = _Ctx(guild)

    await cog.mutesync(cog, ctx)

    for channel in (text, forum):
        assert channel.applied, f"{channel.name} never received the fix"
        assert set(THREAD_PERMS) <= _denied(channel.applied[-1][1])


async def test_resyncing_an_already_correct_guild_costs_no_api_calls(fake_pool):
    """Idempotent AND free: a 500-channel guild must not pay 500 edits to change
    nothing (and eat the rate limit doing it)."""
    role = _Role()
    text = _Text("general")
    guild = _RoleGuild([text], role)
    cog = _sync_cog(fake_pool, role.id)

    await cog.mutesync(cog, _Ctx(guild))
    first = len(text.applied)
    await cog.mutesync(cog, _Ctx(guild))

    assert first == 1
    assert len(text.applied) == 1


async def test_resyncing_never_widens_an_unrelated_deny(fake_pool):
    """A security fix that silently opens a staff-only channel is not a fix.

    ``set_permissions(role, overwrite=...)`` REPLACES the overwrite, so writing
    ours flat would drop whatever else the server had put there by hand.
    """
    role = _Role()
    text = _Text("staff", existing=discord.PermissionOverwrite(view_channel=False))
    guild = _RoleGuild([text], role)
    cog = _sync_cog(fake_pool, role.id)

    await cog.mutesync(cog, _Ctx(guild))

    written = text.applied[-1][1]
    assert written.view_channel is False, "the guild's own deny was wiped"
    assert set(THREAD_PERMS) <= _denied(written)


async def test_a_channel_that_refuses_the_edit_does_not_abort_the_rest(fake_pool):
    class _Hostile(_Text):
        async def set_permissions(self, role, *, overwrite=None, **kwargs):
            raise discord.HTTPException(
                types.SimpleNamespace(status=403, reason="test"), "nope"
            )

    role = _Role()
    hostile = _Hostile("locked")
    ok = _Text("general")
    guild = _RoleGuild([hostile, ok], role)
    cog = _sync_cog(fake_pool, role.id)

    applied, failed = await cog._sync_mute_overwrites(guild, role)

    assert (applied, failed) == (1, 1)
    assert ok.applied


async def test_mutesync_says_so_when_there_is_no_role_yet(fake_pool):
    guild = _RoleGuild([], _Role())
    cog = _sync_cog(fake_pool, None)
    ctx = _Ctx(guild)

    await cog.mutesync(cog, ctx)

    assert ctx.sent and "mute role" in ctx.sent[0].lower()


# --- leg two: every NEW channel used to re-open the hole --------------------
def _events_cog(role, muteroles):
    from cogs.system import events as events_module

    bot = types.SimpleNamespace(muteroles=muteroles)
    cog = events_module.Events.__new__(events_module.Events)
    cog.bot = bot
    return cog


async def test_a_channel_created_after_the_mute_is_denied_threads_too():
    """The listener used to re-apply the OLD inline shape, so in a guild where
    the mute role was correct, every channel created afterwards re-opened it."""
    role = _Role()
    channel = _Text("brand-new")
    channel.guild = _RoleGuild([channel], role)
    cog = _events_cog(role, {channel.guild.id: role.id})

    await cog.on_guild_channel_create(channel)

    assert channel.applied
    assert set(THREAD_PERMS) <= _denied(channel.applied[-1][1])


async def test_a_forum_created_after_the_mute_is_muted_at_all():
    """A forum is nothing but threads and the old listener had no branch for
    one, so a new forum came up 100% unmuted."""
    role = _Role()
    forum = _Forum("new-help")
    forum.guild = _RoleGuild([forum], role)
    cog = _events_cog(role, {forum.guild.id: role.id})

    await cog.on_guild_channel_create(forum)

    assert forum.applied, "a new forum got no overwrite at all"
    assert set(THREAD_PERMS) <= _denied(forum.applied[-1][1])


async def test_a_new_voice_channel_denies_its_text_chat():
    role = _Role()
    voice = _Voice("new-room")
    voice.guild = _RoleGuild([voice], role)
    cog = _events_cog(role, {voice.guild.id: role.id})

    await cog.on_guild_channel_create(voice)

    denied = _denied(voice.applied[-1][1])
    assert {"speak", "send_messages"} <= denied


async def test_a_guild_with_no_mute_role_still_costs_the_listener_nothing():
    channel = _Text("brand-new")
    channel.guild = _RoleGuild([channel], _Role())
    cog = _events_cog(None, {})

    await cog.on_guild_channel_create(channel)

    assert channel.applied == []
