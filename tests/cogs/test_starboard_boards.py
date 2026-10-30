"""V5c-B: several starboards per server (Yasuho+), board 1 untouched.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import types

from cogs.config.starboard import STAR, Board, Starboard, parse_emoji
from tools import premium

HEART = "\U0001F496"
UTC = datetime.timezone.utc


class _Role:
    id = 42


class _Guild:
    id = 42
    default_role = _Role()
    roles = [default_role]

    def __init__(self):
        self.channels = {}

    def get_channel(self, cid):
        return self.channels.get(cid)

    get_channel_or_thread = get_channel


class _Chan:
    def __init__(self, cid, guild):
        self.id = cid
        self.guild = guild
        self.sent = []
        guild.channels[cid] = self

    def permissions_for(self, obj):
        return types.SimpleNamespace(view_channel=True)

    def is_nsfw(self):
        return False

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(id=900 + len(self.sent))


class _Pool:
    def __init__(self):
        self.executed = []
        self.fetched = 0

    async def fetchrow(self, query, *args):
        self.fetched += 1
        return None

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))


def _msg(emoji, count):
    return types.SimpleNamespace(
        id=100,
        reactions=[types.SimpleNamespace(emoji=emoji, count=count)],
        author=types.SimpleNamespace(
            display_name="V", display_avatar=types.SimpleNamespace(url="https://x/a.png")
        ),
        channel=types.SimpleNamespace(mention="#c"),
        jump_url="https://discord.com/channels/42/3/100",
        created_at=datetime.datetime.now(UTC),
    )


def _setup(limits, *, main=True, count=3, emoji=HEART):
    guild = _Guild()
    src = _Chan(3, guild)
    main_ch = _Chan(9, guild)
    extra_ch = _Chan(10, guild)
    pool = _Pool()
    msg = _msg(emoji, count)
    bot = types.SimpleNamespace(
        db_pool=pool,
        cached_messages=[msg],
        get_guild=lambda gid: guild if gid == 42 else None,
        premium=types.SimpleNamespace(for_guild=lambda gid: limits),
    )
    cog = Starboard(bot)
    cog._config[42] = (9, 3) if main else None
    cog._extra[42] = {HEART: Board(2, 10, HEART, 3)}
    return cog, src, main_ch, extra_ch, pool


def _payload(emoji):
    return types.SimpleNamespace(emoji=emoji, message_id=100, channel_id=3, guild_id=42)


async def test_extra_board_posts_to_its_own_channel_with_its_slot_and_emoji():
    cog, _src, main_ch, extra_ch, pool = _setup(premium.GUILD_PREMIUM)

    await cog.handle(_payload(HEART))

    assert len(extra_ch.sent) == 1 and main_ch.sent == []
    assert extra_ch.sent[0]["embed"].footer.text == f"3 {HEART}"
    insert = [a for q, a in pool.executed if q.startswith("INSERT INTO starboard_entries")]
    assert insert and insert[0][-1] == 2  # slot
    # The post links to the message, never copies it.
    assert "Jump" in extra_ch.sent[0]["embed"].fields[0].value


async def test_lapsed_guild_keeps_the_extra_board_configured_but_silent():
    cog, _src, _m, extra_ch, pool = _setup(premium.GUILD_FREE)

    await cog.handle(_payload(HEART))

    assert extra_ch.sent == [] and pool.executed == []
    assert cog._extra[42][HEART].channel_id == 10  # nothing deleted


async def test_board_one_stays_active_when_lapsed():
    cog, _src, main_ch, _e, _p = _setup(premium.GUILD_FREE, emoji=STAR)

    await cog.handle(_payload(STAR))

    assert len(main_ch.sent) == 1


async def test_extra_board_needs_the_main_board():
    """The dashboard can delete board 1: extras must not outlive it."""
    cog, _src, _m, extra_ch, _p = _setup(premium.GUILD_PREMIUM, main=False)

    await cog.handle(_payload(HEART))

    assert extra_ch.sent == []


async def test_other_emoji_in_a_one_board_server_costs_nothing():
    """The hot path: a dict miss, zero queries (the pool counts and would show them)."""
    cog, _src, *_rest, pool = _setup(premium.GUILD_PREMIUM)
    cog._extra.clear()

    await cog.handle(_payload("\U0001F44D"))

    assert pool.fetched == 0 and pool.executed == []


async def test_below_threshold_on_an_extra_board_posts_nothing():
    cog, _src, _m, extra_ch, _p = _setup(premium.GUILD_PREMIUM, count=2)

    await cog.handle(_payload(HEART))

    assert extra_ch.sent == []


def test_parse_emoji():
    assert parse_emoji(HEART) == HEART
    assert parse_emoji("<:pog:123456789012345678>") == "<:pog:123456789012345678>"
    assert parse_emoji("<a:pog:123456789012345678>")
    for bad in ("", "abc", ":smile:", "a b", HEART * 20, "<:x:1>"):
        assert parse_emoji(bad) is None


# -- /starboard add ------------------------------------------------------
class _AddPool(_Pool):
    async def fetchrow(self, query, *args):
        if query.lstrip().startswith("INSERT"):
            self.executed.append((" ".join(query.split()), args))
            return {"slot": args[1]}
        return None

    async def fetch(self, query, *args):
        return []


class _Ctx:
    def __init__(self):
        self.guild = types.SimpleNamespace(id=42)
        self.author = object()
        self.sent = []

    async def defer(self):
        pass

    async def send(self, *args, **kwargs):
        self.sent.append(args[0] if args else kwargs)


def _add_cog(limits, taken=()):
    bot = types.SimpleNamespace(
        db_pool=_AddPool(),
        premium=types.SimpleNamespace(for_guild=lambda gid: limits),
    )
    cog = Starboard(bot)
    cog._config[42] = (9, 3)
    cog._extra[42] = {f"e{s}": Board(s, 10, f"e{s}", 3) for s in taken}
    refusals = []

    async def refuse(ctx, text):
        refusals.append(text)

    cog._refuse_boards = refuse
    return cog, refusals


async def _add(cog, emoji=HEART):
    ctx = _Ctx()
    channel = types.SimpleNamespace(id=10, mention="#b")
    await cog.starboard_add.callback(cog, ctx, channel, emoji, 3)
    return ctx


async def test_add_takes_the_lowest_free_slot():
    cog, refusals = _add_cog(premium.GUILD_PREMIUM, taken=(2, 4))
    await _add(cog)
    insert = cog.bot.db_pool.executed[0][1]
    assert insert[1] == 3 and refusals == []


async def test_add_is_refused_at_the_limit_with_the_upsell():
    cog, refusals = _add_cog(premium.GUILD_PREMIUM, taken=(2, 3, 4, 5))
    await _add(cog)
    assert len(refusals) == 1 and cog.bot.db_pool.executed == []


async def test_free_guild_cannot_add_any_extra_board():
    cog, refusals = _add_cog(premium.GUILD_FREE)
    await _add(cog)
    assert len(refusals) == 1 and cog.bot.db_pool.executed == []


async def test_add_refuses_star_and_duplicates_and_missing_main_board():
    cog, refusals = _add_cog(premium.GUILD_PREMIUM, taken=(2,))
    cog._extra[42][HEART] = Board(3, 10, HEART, 3)
    for emoji in (STAR, HEART, "plain"):
        ctx = await _add(cog, emoji)
        assert ctx.sent and cog.bot.db_pool.executed == []
    cog._config[42] = None
    ctx = await _add(cog)
    assert cog.bot.db_pool.executed == [] and refusals == []


# -- review fixes ------------------------------------------------------------
from cogs.config.starboard import emoji_key  # noqa: E402

CUSTOM_ID = 123456789012345678


def test_custom_emoji_is_keyed_by_id_whatever_the_name_or_animation():
    add = types.SimpleNamespace(id=CUSTOM_ID, name="pog", animated=True)
    remove = types.SimpleNamespace(id=CUSTOM_ID, name="pog", animated=False)
    stored = f"<a:pog:{CUSTOM_ID}>"
    assert emoji_key(add) == emoji_key(remove) == emoji_key(stored) == str(CUSTOM_ID)
    assert emoji_key(f"<:renamed:{CUSTOM_ID}>") == str(CUSTOM_ID)


def test_unicode_variation_selector_is_ignored_on_both_sides():
    assert emoji_key(HEART + "️") == emoji_key(HEART)
    assert parse_emoji(HEART + "️") == HEART
    assert emoji_key(STAR + "️") == STAR


async def test_custom_board_matches_an_event_with_a_different_animated_flag():
    cog, _src, _m, extra_ch, _p = _setup(premium.GUILD_PREMIUM)
    stored = f"<a:pog:{CUSTOM_ID}>"
    cog._extra[42] = {emoji_key(stored): Board(2, 10, stored, 3)}
    event = types.SimpleNamespace(id=CUSTOM_ID, name="pog", animated=False)
    cog.bot.cached_messages[0].reactions = [
        types.SimpleNamespace(emoji=types.SimpleNamespace(id=CUSTOM_ID, name="pog", animated=False), count=3)
    ]
    await cog.handle(_payload(event))
    assert len(extra_ch.sent) == 1


async def test_a_non_star_reaction_never_reaches_the_config_or_the_db():
    """Hot path: nothing is awaited for an unrelated emoji, even on an unloaded guild."""
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("the database must not be touched")

    cog = Starboard(types.SimpleNamespace(db_pool=Boom()))
    await cog.handle(_payload("\U0001F44D"))
    await cog.handle(types.SimpleNamespace(emoji=HEART, message_id=1, channel_id=1, guild_id=77))


async def test_reacting_inside_any_board_channel_is_ignored():
    for channel_id in (9, 10):  # board 1 and the extra board
        cog, _src, main_ch, extra_ch, pool = _setup(premium.GUILD_PREMIUM)
        payload = types.SimpleNamespace(
            emoji=HEART, message_id=100, channel_id=channel_id, guild_id=42
        )
        await cog.handle(payload)
        assert main_ch.sent == [] and extra_ch.sent == [] and pool.executed == []


async def test_dashboard_starboard_invalidation_reloads_the_extras():
    from cogs.system import dashboard_sync

    class Pool:
        async def fetchrow(self, query, *args):
            return None  # the dashboard deleted board 1

        async def fetch(self, query, *args):
            return []  # ... and the FK cascade emptied the extras

    bot = types.SimpleNamespace(db_pool=Pool())
    cog = Starboard(bot)
    cog._extra[42] = {HEART: Board(2, 10, HEART, 3)}
    bot.get_cog = lambda name: cog
    await dashboard_sync._invalidate_starboard(bot, 42)
    assert 42 not in cog._extra and cog._config[42] is None
