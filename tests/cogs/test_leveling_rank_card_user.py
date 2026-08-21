"""The PER-MEMBER rank-card layer (lot U1): precedence, cost, and the switch.

Four things are pinned here, in ascending order of how expensive a regression
would be:

* the READ COST - the acceptance bar of this lot. A member with no personal card
  (the overwhelming majority) must cost the /rank render NOTHING it did not cost
  before U1 existed: zero queries, zero awaits, once their marker is warm. A
  member WITH one costs exactly ONE statement, carrying the accent and the bytes
  together;
* PRECEDENCE - user background > guild background > stock, user accent > guild
  accent > role colour, decided per knob (a member with only an accent still
  gets the guild's background under it);
* the KILL SWITCH - a guild that refuses member styles renders exactly what RC1
  rendered, and the member layer is not even READ there;
* the WRITE SEAM and the RGPD wiring - every bot-side write re-marks the cache in
  the same call, and an out-of-band erasure (tools/privacy) needs no hook at all
  because the marker is self-healing.

The stock card itself is guarded by the golden hash in
tests/cogs/test_leveling_rank_card.py; what is pinned HERE is that a member and
guild with nothing set hand the renderer the exact same arguments as before, so
that hash still describes what /rank draws.

No network, no database, no Discord: a fake bot holding a recording pool, the
same discipline as tests/cogs/test_leveling_rank_card.py.
"""

from __future__ import annotations

import io
import types

import pytest
from PIL import Image

import cogs.community.leveling.leveling as leveling_module
import cogs.community.leveling.rank_card_user as rank_card_user
from cogs.community.leveling import rank_card
from cogs.community.leveling.leveling import Leveling
from tools import settings


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """tools.settings caches guild blobs for the whole process, and the upload
    ceiling is module state: a test that warmed either would otherwise decide
    the next one's answer."""
    settings.invalidate_all()
    rank_card_user._INFLIGHT_UPLOADS.count = 0
    yield
    settings.invalidate_all()
    rank_card_user._INFLIGHT_UPLOADS.count = 0


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _background(colour=(20, 200, 90)):
    """A real, stored-shaped background: card-sized WebP, as U1 persists it."""
    encoded, _stored_format = rank_card.validate_and_downscale(
        _png(Image.new("RGB", (1600, 900), colour))
    )
    return encoded


# ---------------------------------------------------------------------------
# Fake pool: routes by table so every read can be counted per layer.
# ---------------------------------------------------------------------------


class CardPool:
    def __init__(
        self,
        *,
        guild_rows=None,
        guild_backgrounds=None,
        user_cards=None,
        guild_settings=None,
    ):
        # guild_id -> {"accent": ..., "background_format": ..., "has_background": ...}
        self.guild_rows = guild_rows or {}
        self.guild_backgrounds = guild_backgrounds or {}
        # user_id -> {"accent": int|None, "background": bytes|None}; absent =>
        # no row at all, which is a DIFFERENT answer from a row of NULLs.
        self.user_cards = user_cards or {}
        # guild_id -> the settings JSONB blob.
        self.guild_settings = guild_settings or {}
        self.guild_config_reads = 0
        self.guild_background_reads = 0
        self.user_card_reads = 0
        self.settings_reads = 0
        self.executes = []
        self.fail_user = False

    async def fetchrow(self, query, *args):
        if "FROM user_rank_cards" in query:
            self.user_card_reads += 1
            if self.fail_user:
                raise RuntimeError("db down")
            row = self.user_cards.get(args[0])
            if row is None or "background IS NOT NULL" not in query:
                return row
            # The blob-free projection the describing surfaces read.
            return {
                "accent": row["accent"],
                "has_background": row["background"] is not None,
                "updated_at": None,
            }
        if "FROM rank_cards" in query:
            self.guild_config_reads += 1
            return self.guild_rows.get(args[0])
        raise AssertionError("unexpected fetchrow: %s" % query)

    async def fetchval(self, query, *args):
        if "FROM levels" in query:
            # The two reads /rank does before it draws anything: the member's
            # XP total, then their rank position. Canned - this file is about
            # the CARD, and the XP maths has its own tests.
            return 1 if "COUNT(*)" in query else 0
        if "user_rank_cards" in query and "DELETE FROM user_rank_cards" in query:
            # The single-knob clear. MODELLED rather than stubbed - it nulls one
            # column and drops the row when the other one is unset too - so the
            # marker the cog writes is derived from real row semantics instead
            # of from a canned return value.
            self.executes.append((query, args))
            column = "background" if "SET background = NULL" in query else "accent"
            other = "accent" if column == "background" else "background"
            row = self.user_cards.get(args[0])
            if row is None:
                return False
            if row.get(other) is None:
                del self.user_cards[args[0]]
                return False
            row[column] = None
            return True
        if "FROM guild_settings" in query:
            self.settings_reads += 1
            return self.guild_settings.get(args[0])
        if "FROM rank_cards" in query:
            self.guild_background_reads += 1
            return self.guild_backgrounds.get(args[0])
        raise AssertionError("unexpected fetchval: %s" % query)

    async def execute(self, query, *args):
        self.executes.append((query, args))
        if query.startswith("DELETE FROM user_rank_cards"):
            self.user_cards.pop(args[0], None)
        return "UPDATE 1"

    @property
    def reads(self):
        return (
            self.guild_config_reads
            + self.guild_background_reads
            + self.user_card_reads
            + self.settings_reads
        )


def _cog(pool):
    return Leveling(types.SimpleNamespace(db_pool=pool, get_cog=lambda name: None))


# ---------------------------------------------------------------------------
# Driving /rank itself: the command decides WHICH background reaches the
# renderer, so the only honest place to pin that decision is the command.
# ---------------------------------------------------------------------------


class _Avatar:
    url = "https://cdn.example/avatar.png"

    def replace(self, **kwargs):
        return self

    async def read(self):
        return b"avatar-bytes"


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RankCtx:
    """A commands.Context stand-in for /rank, with a channel that answers
    ``permissions_for`` the way the real one does."""

    def __init__(self, cog, *, guild_id=7, author_id=42, may_attach=True):
        self.bot = cog.bot
        self.guild = types.SimpleNamespace(id=guild_id)
        self.author = types.SimpleNamespace(
            id=author_id,
            display_name="Yasuho Hirose",
            display_avatar=_Avatar(),
            # A member with no colour: the stock accent path, so the accent
            # under test is only ever the one the resolver hands back.
            colour=types.SimpleNamespace(value=0, to_rgb=lambda: (0, 0, 0)),
        )
        self.channel = _PermChannel(may_attach)
        self.sends = []

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


def _record_renders(monkeypatch):
    """Capture what /rank hands the renderer, without running Pillow.

    Returns the list the recorder appends to. ``run_image_job`` is collapsed to
    a direct call so the job runs inline (its thread pool and semaphore are
    tools/rendering's business, tested there).
    """
    renders = []

    def _render_rank_card(
        avatar_bytes,
        name,
        level,
        rank_pos,
        xp,
        cur_threshold,
        next_threshold,
        accent,
        background,
    ):
        renders.append({"name": name, "accent": accent, "background": background})
        return io.BytesIO(b"card")

    async def _run_image_job(bot, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        Leveling, "_render_rank_card", staticmethod(_render_rank_card)
    )
    monkeypatch.setattr(
        leveling_module.rendering, "run_image_job", _run_image_job
    )
    return renders


# ---------------------------------------------------------------------------
# Read cost: THE acceptance bar of this lot.
# ---------------------------------------------------------------------------


async def test_a_member_with_no_row_costs_the_render_nothing_once_warm():
    """The common case. One cold lookup, then zero forever - which is exactly
    what a guild with no rank_cards row already costs (RC1)."""
    pool = CardPool()
    cog = _cog(pool)

    assert await cog.resolve_rank_card_render(7, 42) == (None, None)
    cold = pool.reads
    assert pool.user_card_reads == 1  # the one cold marker lookup

    for _ in range(5):
        assert await cog.resolve_rank_card_render(7, 42) == (None, None)

    # Not one further query of ANY kind: guild style cached (RC1), guild blob
    # never fetched (no background), settings blob cached, member marker warm.
    assert pool.reads == cold
    assert pool.user_card_reads == 1


async def test_a_member_with_a_row_costs_exactly_one_statement_per_render():
    """Accent AND bytes in the same round trip - never the guild path's two."""
    blob = _background()
    pool = CardPool(user_cards={42: {"accent": 0xFF0000, "background": blob}})
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)

    assert accent == rank_card.accent_to_rgb(0xFF0000)
    assert background == blob
    assert pool.user_card_reads == 1
    assert pool.guild_background_reads == 0  # the member's blob replaced it

    await cog.resolve_rank_card_render(7, 42)
    assert pool.user_card_reads == 2  # one per render, never two


async def test_the_marker_heals_itself_when_the_row_is_erased_out_of_band():
    """THE reason no erasure path (tools/privacy, the dashboard) has to reach
    into this cog: the cache holds a HINT, never a value."""
    pool = CardPool(user_cards={42: {"accent": 0x5865F2, "background": None}})
    cog = _cog(pool)
    assert (await cog.resolve_rank_card_render(7, 42))[0] == rank_card.accent_to_rgb(
        0x5865F2
    )
    assert cog._user_rank_cards.get(42) is True

    # `?mydata deleteprofile` / `/profile clear` deleted the row behind our back.
    pool.user_cards.clear()

    assert await cog.resolve_rank_card_render(7, 42) == (None, None)
    assert cog._user_rank_cards.get(42) is False
    reads_after_healing = pool.user_card_reads

    await cog.resolve_rank_card_render(7, 42)
    assert pool.user_card_reads == reads_after_healing  # back on the fast path


async def test_a_db_failure_degrades_to_the_stock_style_without_poisoning():
    pool = CardPool()
    pool.fail_user = True
    cog = _cog(pool)

    assert await cog.ensure_user_rank_card_style(42) == (None, None)
    # NOT cached: a hiccup must not pin "this member has nothing" for an hour.
    assert cog._user_rank_cards.get(42) is None


async def test_the_marker_cache_is_bounded():
    pool = CardPool()
    cog = _cog(pool)
    cog._user_rank_cards._capacity = 4
    for user_id in range(20):
        cog._user_rank_cards[user_id] = False
    assert len(cog._user_rank_cards) <= 4
    assert rank_card_user._USER_RANK_CARD_CACHE_CAP >= 1024


# ---------------------------------------------------------------------------
# Precedence.
# ---------------------------------------------------------------------------


async def test_the_member_accent_outranks_the_guild_accent():
    pool = CardPool(
        guild_rows={
            7: {"accent": 0x00FF00, "background_format": None, "has_background": False}
        },
        user_cards={42: {"accent": 0xFF0000, "background": None}},
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)

    # BOTH layers hand the renderer the same type (an rgb tuple), or the loser
    # of this test would be the only one that could actually draw.
    assert accent == rank_card.accent_to_rgb(0xFF0000)
    assert background is None


async def test_the_guild_accent_still_wins_over_the_role_colour():
    """No member row: byte-for-byte the RC1 behaviour, including the rgb tuple."""
    pool = CardPool(
        guild_rows={
            7: {"accent": 0x00FF00, "background_format": None, "has_background": False}
        }
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)

    assert accent == rank_card.accent_to_rgb(0x00FF00)
    assert background is None


async def test_the_two_knobs_are_independent():
    """A member with only an accent keeps the guild's background under it."""
    guild_blob = _background((10, 10, 200))
    pool = CardPool(
        guild_rows={
            7: {"accent": 0x00FF00, "background_format": "webp", "has_background": True}
        },
        guild_backgrounds={7: guild_blob},
        user_cards={42: {"accent": 0xFF0000, "background": None}},
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)

    assert accent == rank_card.accent_to_rgb(0xFF0000)  # the member's
    assert background == guild_blob  # the guild's


async def test_a_member_background_replaces_the_guilds_without_reading_it():
    guild_blob = _background((10, 10, 200))
    member_blob = _background((200, 10, 10))
    pool = CardPool(
        guild_rows={
            7: {"accent": None, "background_format": "webp", "has_background": True}
        },
        guild_backgrounds={7: guild_blob},
        user_cards={42: {"accent": None, "background": member_blob}},
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)

    assert background == member_blob
    assert accent is None  # neither layer overrides: the role colour survives
    # The guild blob is up to 512 KiB; it must not be read just to be discarded.
    assert pool.guild_background_reads == 0


async def test_the_resolved_style_is_what_the_renderer_can_actually_draw():
    """A type guard with teeth. /rank feeds the resolver's accent straight to
    Pillow, so a packed int leaking out of the member layer where the guild
    layer hands back an rgb tuple would not be a cosmetic difference - it would
    crash the render and drop every customised member onto the plain-embed
    fallback. Rendering for real is the only assertion that proves it."""
    avatar = io.BytesIO()
    Image.new("RGBA", (128, 128), (200, 40, 120, 255)).save(avatar, "PNG")
    pool = CardPool(
        user_cards={42: {"accent": 0xFF0000, "background": _background((200, 10, 10))}}
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)
    buffer = Leveling._render_rank_card(
        avatar.getvalue(), "Yasuho Hirose", 12, 3, 15000, 14400, 16900,
        accent, background,
    )

    with Image.open(buffer) as card:
        assert card.size == rank_card.CARD_SIZE


async def test_nothing_configured_anywhere_is_the_stock_card():
    """What keeps the golden hash meaningful: /rank still passes accent=None,
    background=None, i.e. the exact arguments it passed before this lot."""
    pool = CardPool()
    cog = _cog(pool)

    assert await cog.resolve_rank_card_render(7, 42) == (None, None)


# ---------------------------------------------------------------------------
# The kill switch.
# ---------------------------------------------------------------------------


async def test_a_guild_that_refuses_member_styles_never_reads_the_member_layer():
    member_blob = _background()
    pool = CardPool(
        guild_rows={
            7: {"accent": 0x00FF00, "background_format": None, "has_background": False}
        },
        user_cards={42: {"accent": 0xFF0000, "background": member_blob}},
        guild_settings={7: {rank_card.ALLOW_USER_STYLES_KEY: False}},
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(7, 42)

    assert accent == rank_card.accent_to_rgb(0x00FF00)  # RC1, exactly
    assert background is None
    assert pool.user_card_reads == 0  # not read, not just ignored


async def test_the_switch_defaults_to_allowed_and_is_read_from_the_shared_blob():
    pool = CardPool()
    cog = _cog(pool)

    assert await cog.allows_user_rank_card_styles(7) is True
    # tools.settings' per-guild blob is the one the locale lookup already warms,
    # so the render path pays no query of its own for this in steady state.
    warm = pool.settings_reads
    assert await cog.allows_user_rank_card_styles(7) is True
    assert pool.settings_reads == warm


@pytest.mark.parametrize("stored", [None, {}, {"rank_card_allow_user_styles": "off"}])
async def test_anything_but_an_explicit_false_fails_open(stored):
    """A dashboard writing a malformed value must not silently strip every
    member's card - the absent key and a bad one give the same answer."""
    pool = CardPool(guild_settings={} if stored is None else {7: stored})
    cog = _cog(pool)

    assert await cog.allows_user_rank_card_styles(7) is True


async def test_a_settings_failure_fails_open_too():
    class _AngryPool(CardPool):
        async def fetchval(self, query, *args):
            raise RuntimeError("db down")

    cog = _cog(_AngryPool())

    assert await cog.allows_user_rank_card_styles(7) is True


async def test_the_switch_is_written_through_the_seam_that_owns_its_spelling():
    pool = CardPool()
    cog = _cog(pool)

    await cog.set_user_rank_card_styles_allowed(7, False)

    assert any(
        rank_card.ALLOW_USER_STYLES_KEY in args
        for _query, args in pool.executes
    )
    # And the very next render obeys it: tools.settings updated its own blob.
    assert await cog.allows_user_rank_card_styles(7) is False


# ---------------------------------------------------------------------------
# Validation reuse: a member cannot store what an admin could not.
# ---------------------------------------------------------------------------


async def _fake_run_image_job(bot, function, *args, **kwargs):
    return function(*args, **kwargs)


async def test_the_member_upload_runs_the_same_pipeline_as_the_admin_one(
    monkeypatch,
):
    pool = CardPool()
    cog = _cog(pool)
    calls = []

    async def _recording(bot, function, *args, **kwargs):
        calls.append(function)
        return await _fake_run_image_job(bot, function, *args, **kwargs)

    monkeypatch.setattr(rank_card_user.rendering, "run_image_job", _recording)

    raw = _png(Image.new("RGB", (1600, 900), (10, 200, 90)))
    await cog.set_user_rank_background(42, raw, "image/png")

    # The SAME function the guild seam routes through - not a copy of it.
    assert calls == [rank_card.validate_and_downscale]
    inserts = [c for c in pool.executes if "INSERT INTO user_rank_cards" in c[0]]
    assert len(inserts) == 1
    stored = inserts[0][1][1]
    # Normalised on the way IN: card-sized, under the stored cap, never the
    # 1600x900 source.
    with Image.open(io.BytesIO(stored)) as image:
        assert image.size == rank_card.CARD_SIZE
    assert len(stored) <= rank_card.MAX_STORED_BYTES
    assert cog._user_rank_cards.get(42) is True


@pytest.mark.parametrize(
    "data, content_type, expected",
    [
        (b"x" * (rank_card.MAX_SOURCE_BYTES + 1), None, rank_card.SourceTooLarge),
        (b"not an image", "application/zip", rank_card.UnsupportedFormat),
        (b"not an image", None, rank_card.DecodeFailed),
    ],
)
async def test_a_rejected_member_upload_writes_nothing(
    monkeypatch, data, content_type, expected
):
    pool = CardPool()
    cog = _cog(pool)
    monkeypatch.setattr(
        rank_card_user.rendering, "run_image_job", _fake_run_image_job
    )

    with pytest.raises(expected):
        await cog.set_user_rank_background(42, data, content_type)

    assert pool.executes == []
    assert cog._user_rank_cards.get(42) is None


async def test_the_member_accent_uses_the_shared_parser():
    pool = CardPool()
    cog = _cog(pool)

    assert await cog.set_user_rank_accent(42, "#58F") == 0xFFFFFF & 0x5588FF
    with pytest.raises(rank_card.InvalidAccent):
        await cog.set_user_rank_accent(42, "#12345")

    inserts = [c for c in pool.executes if "INSERT INTO user_rank_cards" in c[0]]
    assert len(inserts) == 1  # the rejection wrote nothing
    assert inserts[0][1] == (42, 0x5588FF)
    assert cog._user_rank_cards.get(42) is True


# ---------------------------------------------------------------------------
# The write seam: every write re-marks the cache in the SAME call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, card, expected_fragment, expected_marker",
    [
        # A full clear always deletes the row, so the marker goes to the free
        # path.
        (
            {},
            {"accent": 0x5588FF, "background": b"png"},
            "DELETE FROM user_rank_cards WHERE user_id = $1;",
            False,
        ),
        # One knob cleared while the OTHER one is still set: the row survives
        # and the marker says so.
        (
            {"target": "background"},
            {"accent": 0x5588FF, "background": b"png"},
            "SET background = NULL",
            True,
        ),
        (
            {"target": "accent"},
            {"accent": 0x5588FF, "background": b"png"},
            "SET accent = NULL",
            True,
        ),
        # One knob cleared when it was the ONLY one set: the row would be left
        # empty, so it is deleted instead and the marker stays on the free path.
        # A surviving row of NULLs would cost this member one pointless query on
        # every /rank until the cache evicted them.
        (
            {"target": "background"},
            {"accent": None, "background": b"png"},
            "SET background = NULL",
            False,
        ),
        (
            {"target": "accent"},
            {"accent": 0x5588FF, "background": None},
            "SET accent = NULL",
            False,
        ),
        # A member who never had a row at all is marked truthfully, not
        # optimistically.
        ({"target": "background"}, None, "SET background = NULL", False),
        ({"target": "accent"}, None, "SET accent = NULL", False),
    ],
)
async def test_clear_writes_the_right_statement_and_re_marks(
    kwargs, card, expected_fragment, expected_marker
):
    pool = CardPool(user_cards={42: dict(card)} if card else {})
    cog = _cog(pool)

    await cog.clear_user_rank_card(42, **kwargs)

    assert any(expected_fragment in query for query, _args in pool.executes)
    assert cog._user_rank_cards.get(42) is expected_marker
    if expected_marker is False:
        # Whatever the path, nothing is left behind for the next render to find.
        assert pool.user_cards.get(42) is None


async def test_a_cleared_member_renders_free_from_then_on():
    """The point of the whole fix: clearing the only knob a member had set must
    not park them on the slow path for the life of the process."""
    pool = CardPool(user_cards={42: {"accent": None, "background": b"png"}})
    cog = _cog(pool)

    await cog.clear_user_rank_card(42, target="background")
    before = pool.user_card_reads
    assert await cog.ensure_user_rank_card_style(42) == (None, None)

    assert pool.user_card_reads == before  # zero queries, not one


def test_the_single_knob_clears_can_never_leave_an_empty_row():
    """Both sub-statements target the same key, so the ONLY thing that makes the
    statement deterministic is that their predicates are mutually exclusive (a
    row reached by both is what Postgres leaves unspecified for data-modifying
    CTEs)."""
    for query, other in (
        (rank_card._CLEAR_USER_BACKGROUND, "accent"),
        (rank_card._CLEAR_USER_ACCENT, "background"),
    ):
        assert "%s IS NULL" % other in query
        assert "%s IS NOT NULL" % other in query


def test_every_user_statement_is_scoped_to_its_owner():
    """No member write may reach another member's row, and each is ONE
    statement (asyncpg's execute is mono-statement)."""
    for query in (
        rank_card.USER_CARD_QUERY,
        rank_card.USER_CONFIG_QUERY,
        rank_card._CLEAR_USER_BACKGROUND,
        rank_card._CLEAR_USER_ACCENT,
    ):
        assert "WHERE user_id = $1" in query
        assert query.count(";") == 1


# ---------------------------------------------------------------------------
# The member-facing surface.
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, guild_id=7, author_id=42):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.author = types.SimpleNamespace(id=author_id)
        self.command = types.SimpleNamespace(reset_cooldown=lambda ctx: None)
        self.sends = []
        self.deferred = False
        self.typing_depth = 0

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))

    async def defer(self, *args, **kwargs):
        self.deferred = True

    def typing(self, **kwargs):
        ctx = self

        class _Typing:
            async def __aenter__(self):
                ctx.typing_depth += 1

            async def __aexit__(self, *exc):
                ctx.typing_depth -= 1

        return _Typing()


class _Attachment:
    def __init__(self, size=100, content_type="image/png", data=None):
        self.size = size
        self.content_type = content_type
        self._data = data if data is not None else _png(
            Image.new("RGB", (400, 400), (1, 2, 3))
        )
        self.read_calls = 0

    async def read(self):
        self.read_calls += 1
        return self._data


async def test_an_oversized_attachment_is_refused_before_downloading():
    pool = CardPool()
    cog = _cog(pool)
    ctx = _Ctx()
    attachment = _Attachment(size=rank_card.MAX_SOURCE_BYTES + 1)

    await cog.rankcard_set_background.callback(cog, ctx, attachment)

    assert attachment.read_calls == 0
    assert pool.executes == []
    assert any("too large" in c[0][0] for c in ctx.sends)


async def test_the_bot_wide_ceiling_refuses_fast_and_refunds_the_cooldown(
    monkeypatch,
):
    """SCALE STORY layer 2: every member of every guild can reach this command,
    and each in-flight call holds up to 8 MiB while it queues for one of the two
    bot-wide render slots. Over the ceiling it is refused, not queued - and the
    refusal is the BOT's fault, so it must not also burn the member's window."""
    pool = CardPool()
    cog = _cog(pool)
    monkeypatch.setattr(
        rank_card_user._INFLIGHT_UPLOADS, "count", rank_card_user._MAX_INFLIGHT_UPLOADS
    )
    refunds = []
    ctx = _Ctx()
    ctx.command = types.SimpleNamespace(reset_cooldown=refunds.append)
    attachment = _Attachment()

    await cog.rankcard_set_background.callback(cog, ctx, attachment)

    assert attachment.read_calls == 0
    assert refunds == [ctx]
    assert any("Too many" in c[0][0] for c in ctx.sends)


async def test_a_successful_upload_releases_its_slot(monkeypatch):
    pool = CardPool()
    cog = _cog(pool)
    monkeypatch.setattr(
        rank_card_user.rendering, "run_image_job", _fake_run_image_job
    )
    ctx = _Ctx()

    await cog.rankcard_set_background.callback(cog, ctx, _Attachment())

    assert rank_card_user._INFLIGHT_UPLOADS.count == 0
    assert any("INSERT INTO user_rank_cards" in q for q, _a in pool.executes)


async def test_a_rejected_upload_releases_its_slot_and_says_why(monkeypatch):
    pool = CardPool()
    cog = _cog(pool)
    monkeypatch.setattr(
        rank_card_user.rendering, "run_image_job", _fake_run_image_job
    )
    ctx = _Ctx()

    await cog.rankcard_set_background.callback(
        cog, ctx, _Attachment(data=b"not an image", content_type=None)
    )

    assert rank_card_user._INFLIGHT_UPLOADS.count == 0
    assert pool.executes == []
    assert any("valid" in c[0][0] for c in ctx.sends)


async def test_view_describes_the_card_and_warms_the_marker():
    pool = CardPool(user_cards={42: {"accent": 0x5865F2, "background": None}})
    cog = _cog(pool)
    ctx = _Ctx()

    await cog.rankcard_view.callback(cog, ctx)

    embed = ctx.sends[0][1]["embed"]
    assert "#5865F2" in embed.fields[1].value
    # A read is an observation: the render fast path is now warm rather than
    # cold, and it costs the BLOB-FREE query to say so.
    assert cog._user_rank_cards.get(42) is True


async def test_view_says_when_this_server_ignores_the_style():
    pool = CardPool(
        user_cards={42: {"accent": 0x5865F2, "background": None}},
        guild_settings={7: {rank_card.ALLOW_USER_STYLES_KEY: False}},
    )
    cog = _cog(pool)
    ctx = _Ctx()

    await cog.rankcard_view.callback(cog, ctx)

    embed = ctx.sends[0][1]["embed"]
    assert any("still applies everywhere else" in field.value for field in embed.fields)


@pytest.mark.parametrize(
    "part, fragment",
    [
        (None, "DELETE FROM user_rank_cards"),
        ("background", "SET background = NULL"),
        ("accent", "SET accent = NULL"),
    ],
)
async def test_the_clear_command_maps_its_argument(part, fragment):
    pool = CardPool()
    cog = _cog(pool)
    ctx = _Ctx()

    await cog.rankcard_clear.callback(cog, ctx, part)

    assert any(fragment in query for query, _args in pool.executes)


def test_the_surface_lives_on_the_leveling_cog_next_to_rank():
    """A hybrid group's subcommands must live in the same cog as their parent,
    and /rankcard customises exactly what /rank draws - so the mixin's commands
    are registered under Leveling (and inherit its help category) rather than
    minting an eighth cog nobody filed under a taxonomy."""
    names = {command.qualified_name for command in Leveling.__cog_commands__}
    assert "rankcard" in names
    assert "rank" in names


# ---------------------------------------------------------------------------
# Attach Files: a member-supplied image is a member-supplied image, even when
# the bot is the one posting it.
# ---------------------------------------------------------------------------


class _Perms:
    def __init__(self, attach_files):
        self.attach_files = attach_files


class _PermChannel:
    """A channel that resolves permissions per member, like the real one."""

    def __init__(self, granted=True, raises=False):
        self.granted = granted
        self.raises = raises

    def permissions_for(self, member):
        if self.raises:
            raise RuntimeError("parent channel not cached")
        return _Perms(self.granted)


def test_may_attach_files_reads_the_channels_answer():
    assert rank_card_user.may_attach_files(_PermChannel(True), object()) is True
    assert rank_card_user.may_attach_files(_PermChannel(False), object()) is False


def test_may_attach_files_fails_closed_when_it_cannot_tell():
    """The opposite default from serverstats' bot-side check, on purpose: that
    one risks losing a chart nobody could otherwise get, this one risks putting
    a member's picture in a channel that refused it. Thread.permissions_for can
    raise (uncached parent), so a raise is one of the ways it cannot tell."""
    assert rank_card_user.may_attach_files(_PermChannel(raises=True), object()) is False
    assert rank_card_user.may_attach_files(object(), object()) is False
    assert rank_card_user.may_attach_files(_PermChannel(True), None) is False


async def test_the_member_image_is_dropped_but_the_member_accent_is_not():
    """The seam. Refusing the image must not cost the member their accent - a
    colour is a number they picked, not a file they uploaded - and the guild's
    own background (staff-chosen, identical for everyone) still shows through
    underneath, exactly as it does for a member with no card at all."""
    guild_blob = _background((10, 10, 200))
    member_blob = _background((200, 10, 10))
    pool = CardPool(
        guild_rows={
            7: {"accent": None, "background_format": "webp", "has_background": True}
        },
        guild_backgrounds={7: guild_blob},
        user_cards={42: {"accent": 0xFF0000, "background": member_blob}},
    )
    cog = _cog(pool)

    accent, background = await cog.resolve_rank_card_render(
        7, 42, allow_user_background=False
    )

    assert background == guild_blob  # not the member's
    assert accent == rank_card.accent_to_rgb(0xFF0000)  # still the member's


async def test_rank_refuses_a_member_background_where_the_invoker_cannot_attach(
    monkeypatch,
):
    """P3, end to end. /rank paints a member-uploaded image into whatever
    channel it was run in, so a member with Attach Files taken away could use
    the bot as a courier for their own picture - and could do it in someone
    else's name with ``/rank @them``. The card still renders; only the
    member-supplied half of it is left out."""
    member_blob = _background((200, 10, 10))
    pool = CardPool(user_cards={42: {"accent": None, "background": member_blob}})
    cog = _cog(pool)
    renders = _record_renders(monkeypatch)

    ctx = _RankCtx(cog, guild_id=7, author_id=42, may_attach=False)
    await Leveling.rank.callback(cog, ctx, ctx.author)

    assert len(renders) == 1, "the card must still render, just without the image"
    assert renders[0]["background"] is None
    assert ctx.sends and "file" in ctx.sends[0][1]  # a card, not the fallback embed


async def test_rank_still_paints_the_member_background_when_the_invoker_may_attach(
    monkeypatch,
):
    """The counter-test: the gate must not quietly strip every card. A member
    who could have posted the image themselves gets it."""
    member_blob = _background((200, 10, 10))
    pool = CardPool(user_cards={42: {"accent": None, "background": member_blob}})
    cog = _cog(pool)
    renders = _record_renders(monkeypatch)

    ctx = _RankCtx(cog, guild_id=7, author_id=42, may_attach=True)
    await Leveling.rank.callback(cog, ctx, ctx.author)

    assert renders[0]["background"] == member_blob


def test_the_render_seam_delegates_to_one_resolver():
    """/rank must not grow its own copy of the precedence rules: the whole
    decision (both layers, the switch, every degradation) is one call."""
    import inspect

    source = inspect.getsource(leveling_module.Leveling.rank.callback)
    assert "resolve_rank_card_render" in source
    assert "fetch_background" not in source
