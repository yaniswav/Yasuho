"""PER-MEMBER rank-card customisation (lot U1): the ``/rankcard`` surface.

The member-facing twin of ``/levelconfig card``. A member sets their own
background and/or accent ONCE and it follows them into every server that allows
it; a server that would rather keep its own branding (or has seen an image it
does not want on its cards) turns the whole layer off with one switch and needs
no write on anybody's row to do it.

PRECEDENCE, resolved per render by :meth:`RankCardUserMixin.resolve_rank_card_render`:

    user background > guild background > the stock panel
    user accent     > guild accent     > the member's role colour

Independent per knob: a member with only an accent still gets the guild's
background under it, and a member with only a background still gets the guild's
accent on top of it. The stock card is untouched by all of this - no row
anywhere means the exact bytes /rank rendered before this lot existed (pinned by
the golden hash in tests/cogs/test_leveling_rank_card.py).

THE KILL SWITCH. ``guild_settings['rank_card_allow_user_styles']`` (the key is
declared in :mod:`cogs.community.leveling.rank_card`, next to the storage it
gates, because the dashboard writes the same key). ABSENT MEANS ALLOWED, so the
feature is on by default and a guild that never heard of it never had to act.
When a guild turns it off, every /rank in THAT guild renders the guild's card
for everyone - nothing is deleted, so a member's style is intact for their other
servers, and flipping it back needs no re-upload. It is read through
tools.settings, whose per-guild blob is the same one the locale lookup already
warms, so consulting it costs no query of its own on the render path.

CACHE DESIGN, and why it is NOT the guild cache's shape. The guild path caches
the METADATA row per guild and re-reads the blob when it needs it, because a
guild's card is read by every member of that guild and the cache is long-lived.
Here the overwhelmingly common case is the opposite: a member has NO row at all,
and the only thing the render needs to know is that. So this cache holds ONE
BOOLEAN per user - "does this member have a row?" - which means:

* a member with no row costs ZERO awaits and ZERO queries once their marker is
  warm (and the first /rank of theirs after a restart costs exactly one
  primary-key lookup, the same cold-start the guild cache already pays);
* a member WITH a row costs exactly ONE statement per render, which carries the
  accent AND the bytes together (``rank_card.USER_CARD_QUERY``) - never the two
  round trips the guild path takes;
* the marker is SELF-HEALING. A ``True`` that is no longer true (the row was
  erased out of band - tools/privacy's erasure, the dashboard) costs one lookup
  that returns nothing, renders the guild/stock card correctly, and rewrites the
  marker to ``False``. That is why no erasure path has to reach into this cog to
  drop anything, and why no dashboard NOTIFY kind is needed for v1: the only
  thing cached is a hint, never a value, and a wrong hint fixes itself on first
  use.

TWO OPEN CONTRACTS, both for the day the Node dashboard grows this surface (it
writes neither today):

* a dashboard that WRITES ``user_rank_cards`` needs a new user-scoped NOTIFY
  kind routed to :meth:`RankCardUserMixin.invalidate_user_rank_card`. A ``True``
  marker heals itself; a ``False`` one does not - it IS the fast path - so a
  member who sets their card from the web would keep rendering the stock one
  until their entry aged out;
* a dashboard that WRITES the kill switch must make its ``rank_card`` kind also
  call ``settings.invalidate_guild(gid)`` (cogs/system/dashboard_sync.py's
  ``_invalidate_rank_card`` only evicts the Leveling cog's style cache today,
  which is correct for the ``rank_cards`` row and blind to the guild_settings
  blob this switch lives in).

SCALE STORY (1000+ guilds). The added steady-state cost of the whole feature on
the /rank path is zero queries and zero awaits for the members who never touched
it, one query for the members who did. Nothing is added to the on_message hot
path - none of this is read outside a /rank render. The style cache is a
BoundedLRU of ``int -> bool`` (a few tens of KiB at its cap), so it cannot grow
with the user population. The upload command is gated twice: a per-user
cooldown (fairness) and a bot-wide in-flight ceiling (:data:`_MAX_INFLIGHT_UPLOADS`,
the global ceiling), because unlike the admin command this one is reachable by
EVERY member of EVERY guild and each in-flight call holds up to
``rank_card.MAX_SOURCE_BYTES`` of downloaded image while it waits for one of the
two bot-wide render slots.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import typing

import discord
from discord.ext import commands

from . import rank_card
from tools import rendering, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.lru_cache import BoundedLRU

log = logging.getLogger(__name__)

# Per-user "has a row?" marker cache ceiling. Sized like tools.settings' user
# cap rather than like the guild card cache: user ids are unbounded, and this is
# the map that makes the common case free. An eviction costs one primary-key
# lookup on that member's next /rank, never a wrong card.
_USER_RANK_CARD_CACHE_CAP = 8192

# What a member with no row renders as: no accent override, no background.
_STOCK_USER_STYLE = (None, None)

# The upload cap as it is SPOKEN to members, derived from the single authority
# so the number in a message can never drift from the number the validator
# enforces (the same derivation level_config_ui.py does for the admin command).
MAX_SOURCE_MB = rank_card.MAX_SOURCE_BYTES // (1024 * 1024)

# SCALE STORY, layer 1 (fairness): one background upload per member per 30s.
# Higher than the admin command's 10s on purpose - that one is behind
# manage_guild, this one is not - and still far above any real "let me try this
# picture" session.
_UPLOAD_COOLDOWN_SECONDS = 30

# SCALE STORY, layer 2 (global ceiling): how many member uploads may be in
# flight bot-wide. Each one holds up to MAX_SOURCE_BYTES of downloaded image
# while it queues for one of tools.rendering's 2 render slots, so without a
# ceiling a coordinated burst across guilds would be a memory spike as much as a
# latency one. Refused fast and politely rather than queued: the member loses
# nothing but a second, and /rank, welcome cards and stats renders keep their
# slots. Comfortably above any organic simultaneity (the per-user cooldown means
# 8 in flight is 8 DIFFERENT people in the same second).
_MAX_INFLIGHT_UPLOADS = 8


class _InflightUploads:
    """A fail-fast bot-wide counter for member background uploads.

    Not an ``asyncio.Semaphore``: a semaphore makes the 9th caller WAIT, which is
    exactly what must not happen here (it would hold its 8 MiB while waiting).
    ``acquire`` either takes a slot or refuses immediately.
    """

    def __init__(self, limit=_MAX_INFLIGHT_UPLOADS):
        self.limit = limit
        self.count = 0

    def acquire(self):
        if self.count >= self.limit:
            return False
        self.count += 1
        return True

    def release(self):
        # Floors at zero so a double release (a future refactor) can never lend
        # out more slots than the limit.
        self.count = max(0, self.count - 1)


# Module-level, so the ceiling is genuinely bot-wide and survives the cog being
# rebuilt by a reload (the same posture as level_config_ui's _PREVIEW_DEBOUNCE).
_INFLIGHT_UPLOADS = _InflightUploads()


def rank_card_error_message(exc):
    """Map a typed :class:`~cogs.community.leveling.rank_card.RankCardError` to a
    short, translated message - one clause per failure, so a rejected upload
    always tells its owner WHY, never a bare "something went wrong".

    Lives here rather than in level_config_ui.py (where it was born, in RC2)
    because both surfaces validate through the SAME pipeline and must therefore
    refuse in the same words; that module imports it back under its old private
    name so its call sites read unchanged.
    """
    if isinstance(exc, rank_card.SourceTooLarge):
        return _("That image is too large - the limit is {mb} MB.").format(
            mb=MAX_SOURCE_MB
        )
    if isinstance(exc, rank_card.ImageTooLarge):
        return _(
            "That image has too many pixels to process safely - try a "
            "smaller picture."
        )
    if isinstance(exc, rank_card.UnsupportedFormat):
        return _("Only PNG, JPEG, and WebP images are supported.")
    if isinstance(exc, rank_card.EncodedTooLarge):
        return _(
            "That image is too complex to fit under the storage limit - "
            "try a simpler or smaller picture."
        )
    if isinstance(exc, rank_card.DecodeFailed):
        return _(
            "I couldn't read that as an image - make sure it's a valid "
            "PNG, JPEG, or WebP file."
        )
    if isinstance(exc, rank_card.InvalidAccent):
        return _(
            "That's not a valid hex colour - try something like #5865F2 "
            "or #58F."
        )
    # Defensive only: every RankCardError subclass is handled above.
    return _("That image couldn't be used for the rank card.")


class RankCardUserMixin:
    """The per-user rank-card layer, folded into the ``Leveling`` cog.

    A MIXIN, not a cog of its own, for two reasons that both point the same way:
    ``/rankcard`` belongs in the same help category and the same cog as ``/rank``
    (it customises exactly what that command draws), and the render resolver
    needs the guild-side accessor (``ensure_rank_card_style``) that lives on that
    cog. The house precedent is cogs/anilist/ (LookupMixin, AiringMixin, ...):
    discord.py's ``CogMeta`` walks the whole MRO, so commands declared here are
    registered under ``Leveling`` exactly as if they had been written in it.

    Everything this adds is namespaced ``*_user_rank_card*`` so nothing here can
    shadow a discord.py base attribute or a sibling mixin's (the house rule from
    tests/test_view_hygiene.py and tests/test_cog_hygiene.py).
    """

    # -- state ----------------------------------------------------------
    def _init_user_rank_card_state(self):
        """Build the per-user style marker cache. Called from ``Leveling.__init__``."""
        # user_id -> bool: "does this member have a user_rank_cards row?".
        # A HINT, never a value - see the module docstring's cache design.
        self._user_rank_cards: BoundedLRU = BoundedLRU(_USER_RANK_CARD_CACHE_CAP)

    # -- reads ----------------------------------------------------------
    async def allows_user_rank_card_styles(self, guild_id):
        """Whether this guild renders per-member card styles. Default True.

        Read through tools.settings, so it shares the per-guild blob cache the
        locale lookup already warms: no query of its own on the render path in
        steady state, and one at worst on a cold blob. Never raises - a settings
        failure falls back to the DEFAULT (allowed), which is the same answer the
        absent key gives, so a hiccup cannot silently strip every member's card.
        """
        try:
            value = await settings.get_guild(
                self.bot.db_pool,
                guild_id,
                rank_card.ALLOW_USER_STYLES_KEY,
                rank_card.ALLOW_USER_STYLES_DEFAULT,
            )
        except Exception:
            log.exception(
                "Failed to read the user-styles switch for guild %s", guild_id
            )
            return rank_card.ALLOW_USER_STYLES_DEFAULT
        # Anything a dashboard might have written that is not a bool: treat only
        # an explicit false as OFF, so a malformed value fails OPEN (allowed),
        # matching the absent-key default.
        return value is not False

    async def ensure_user_rank_card_style(self, user_id):
        """Return this member's ``(accent_rgb, background_bytes)``, or ``(None, None)``.

        THE render accessor. Zero awaits and zero queries when the marker says
        this member has no row (the common case); exactly one statement - accent
        and bytes together - when it says they do, or when the marker is cold.

        The accent comes back UNPACKED into the ``(r, g, b)`` tuple Pillow wants,
        exactly like ``ensure_rank_card_style`` returns the guild's: the two
        layers feed the same renderer argument, so they must speak the same type
        or the loser of the precedence test would be the only one that draws.

        Never raises and never degrades /rank: a DB failure logs and returns the
        stock style WITHOUT touching the marker, so the card still renders (with
        the guild's or the stock look) and the next call retries.
        """
        if self._user_rank_cards.get(user_id) is False:
            return _STOCK_USER_STYLE
        try:
            card = await rank_card.fetch_user_card(self.bot.db_pool, user_id)
        except Exception:
            log.exception("Failed to read the rank card of user %s", user_id)
            return _STOCK_USER_STYLE
        # Self-healing marker: a row that is gone (erasure, dashboard) turns the
        # hint back into the fast path instead of paying this lookup forever.
        self._user_rank_cards[user_id] = card is not None
        if card is None:
            return _STOCK_USER_STYLE
        accent, background = card
        return rank_card.accent_to_rgb(accent), background

    def invalidate_user_rank_card(self, user_id, *, has_row=None):
        """Update (or drop) a member's cached marker after a write.

        ``has_row`` states what the write left behind when the caller knows it
        (a set always leaves a row, a full clear never does), which spares the
        next /rank a lookup; ``None`` drops the entry so the next render
        re-reads. Every bot-side write below calls this in the SAME call, the
        RC2 seam contract applied to the user table.
        """
        if has_row is None:
            self._user_rank_cards.discard(user_id)
        else:
            self._user_rank_cards[user_id] = bool(has_row)

    async def resolve_rank_card_render(self, guild_id, user_id):
        """Return the ``(accent | None, background_bytes | None)`` to draw with.

        THE precedence seam, and the only thing /rank needs to call: it folds the
        guild layer (RC1's cached style plus its blob) and the member layer
        together, in that order of authority, and swallows every read failure
        into "render what we do have" - a cosmetic lookup must never cost a
        member their card.

        ``accent`` is None when neither layer overrides, i.e. the caller keeps
        the member's own role colour (the stock behaviour).
        """
        guild_accent, guild_has_background = await self.ensure_rank_card_style(
            guild_id
        )
        user_accent, background = _STOCK_USER_STYLE
        if await self.allows_user_rank_card_styles(guild_id):
            user_accent, background = await self.ensure_user_rank_card_style(
                user_id
            )
        accent = user_accent if user_accent is not None else guild_accent
        if background is None and guild_has_background:
            # Either the member set no background, or their row vanished between
            # the marker and the read - both mean "fall through to the guild".
            try:
                background = await rank_card.fetch_background(
                    self.bot.db_pool, guild_id
                )
            except Exception:
                log.exception(
                    "Failed to read rank card background for guild %s", guild_id
                )
        return accent, background

    # -- writes (the RC2 seam, user side) --------------------------------
    async def set_user_rank_background(self, user_id, data, content_type=None):
        """Validate, store and re-mark one member's rank-card background.

        ``data`` is the raw uploaded bytes; ``content_type`` is the OPTIONAL
        client-declared type. Raises whichever
        :class:`~cogs.community.leveling.rank_card.RankCardError` subclass the
        upload failed on - nothing is written and the marker is left untouched on
        a rejection. Validation is Pillow work, so it runs through
        tools.rendering.run_image_job like every other image job.
        """
        encoded, _stored_format = await rendering.run_image_job(
            self.bot, rank_card.validate_and_downscale, data, content_type
        )
        await rank_card.set_user_background(self.bot.db_pool, user_id, encoded)
        self.invalidate_user_rank_card(user_id, has_row=True)

    async def set_user_rank_accent(self, user_id, value):
        """Validate, store and re-mark one member's rank-card accent colour.

        Returns the packed 0xRRGGBB int that was stored, for the confirmation
        message. Raises :class:`~cogs.community.leveling.rank_card.InvalidAccent`
        on bad input; nothing is written on a rejection.
        """
        accent = rank_card.validate_accent(value)
        await rank_card.set_user_accent(self.bot.db_pool, user_id, accent)
        self.invalidate_user_rank_card(user_id, has_row=True)
        return accent

    async def clear_user_rank_card(self, user_id, *, target=None):
        """Reset a member's own card customisation and re-mark the cache.

        ``target`` picks what to drop: ``'background'`` and ``'accent'`` keep the
        other knob, ``None`` (the default) deletes the row outright. Idempotent:
        always re-marks, even when there was nothing to clear.

        The single-knob clears mark from what the statement REPORTS rather than
        from an assumption, because clearing one knob does not always leave a row
        behind: the storage helper deletes the row when the other knob is unset
        too (a member who only ever had a background, and a member who never had
        a row at all). Marking those ``True`` would be a lie that costs one
        pointless query on every /rank of theirs until the cache evicts.
        """
        pool = self.bot.db_pool
        if target == "background":
            survived = await rank_card.clear_user_background(pool, user_id)
            self.invalidate_user_rank_card(user_id, has_row=survived)
        elif target == "accent":
            survived = await rank_card.clear_user_accent(pool, user_id)
            self.invalidate_user_rank_card(user_id, has_row=survived)
        else:
            await rank_card.clear_user(pool, user_id)
            self.invalidate_user_rank_card(user_id, has_row=False)

    async def set_user_rank_card_styles_allowed(self, guild_id, allowed):
        """Turn the per-member layer on or off for one guild.

        The write half of :meth:`allows_user_rank_card_styles`, exposed here so
        the admin surface (``/levelconfig card userstyles``) never has to know
        the settings key - one module owns the spelling the dashboard mirrors.
        tools.settings updates its own cache on write, so the next /rank in that
        guild already obeys the new value.
        """
        await settings.set_guild(
            self.bot.db_pool,
            guild_id,
            rank_card.ALLOW_USER_STYLES_KEY,
            bool(allowed),
        )

    # -- the member-facing surface --------------------------------------
    def _user_card_embed(self, accent, has_background, allowed):
        """The '/rankcard view' embed: what is set, and whether it applies here."""
        embed = discord.Embed(
            title=_("Your rank card"),
            colour=discord.Colour(accent) if accent is not None else random_colour(),
        )
        embed.add_field(
            name=_("Background"),
            value=(
                _("Set - your own image, {width}x{height}.").format(
                    width=rank_card.CARD_WIDTH, height=rank_card.CARD_HEIGHT
                )
                if has_background
                else _("Not set - this server's background is used.")
            ),
            inline=False,
        )
        embed.add_field(
            name=_("Accent colour"),
            value=(
                _("Set - {hex}").format(hex="#%06X" % accent)
                if accent is not None
                else _("Not set - your role colour is used.")
            ),
            inline=False,
        )
        if not allowed:
            embed.add_field(
                name=_("Not used here"),
                value=_(
                    "This server keeps its own rank-card look, so your "
                    "personal style is ignored on its cards. It still "
                    "applies everywhere else."
                ),
                inline=False,
            )
        return embed

    async def _send_user_card_state(self, ctx):
        """Read this member's row (blob-free) and describe it."""
        row = await rank_card.fetch_user_config(self.bot.db_pool, ctx.author.id)
        # A read is also an observation: keep the render marker honest with what
        # we just saw, so a /rankcard view warms (or corrects) the fast path.
        self.invalidate_user_rank_card(ctx.author.id, has_row=row is not None)
        allowed = await self.allows_user_rank_card_styles(ctx.guild.id)
        accent = row["accent"] if row is not None else None
        has_background = bool(row["has_background"]) if row is not None else False
        await ctx.send(embed=self._user_card_embed(accent, has_background, allowed))

    @commands.hybrid_group(name="rankcard", aliases=["mycard"])
    @commands.guild_only()
    async def rankcard(self, ctx):
        """Customise your own /rank card: your background and accent colour."""
        if ctx.invoked_subcommand is None:
            await self._send_user_card_state(ctx)

    @rankcard.command(name="view")
    @commands.guild_only()
    async def rankcard_view(self, ctx):
        """Show your personal rank-card style, and whether this server uses it."""
        await self._send_user_card_state(ctx)

    @rankcard.command(name="set-background")
    @commands.guild_only()
    # SCALE STORY, layer 1 (see _UPLOAD_COOLDOWN_SECONDS). Layer 2, the bot-wide
    # ceiling, is taken inside the body - after the cheap rejections, so a
    # refused upload never holds a slot.
    @commands.cooldown(1, _UPLOAD_COOLDOWN_SECONDS, commands.BucketType.user)
    # A describe() string is an English literal read at DEFINITION time (it is
    # not user-locale text and never goes through _()), so interpolating
    # MAX_SOURCE_MB here still yields a plain constant str - and one that cannot
    # drift from the cap the validator enforces.
    @discord.app_commands.describe(
        background=(
            f"An image (PNG, JPEG, or WebP; max {MAX_SOURCE_MB} MB) for YOUR "
            "rank-card background."
        )
    )
    async def rankcard_set_background(
        self, ctx: commands.Context, background: discord.Attachment
    ):
        """Use an image of your own as the background of your rank card.

        Servers can turn personal card styles off; where they have, this image
        is kept but not drawn.
        """
        # Cheap pre-check on the attachment's OWN declared size - refuses an
        # oversized upload before spending a round-trip downloading it (the
        # authoritative check is still validate_and_downscale's byte cap).
        if background.size > rank_card.MAX_SOURCE_BYTES:
            await ctx.send(rank_card_error_message(rank_card.SourceTooLarge()))
            return

        if not _INFLIGHT_UPLOADS.acquire():
            # Refunded on purpose: this refusal is about the BOT being busy, not
            # about this member going too fast, so it must not also cost them
            # their window (the cooldown is charged before the callback runs).
            ctx.command.reset_cooldown(ctx)
            await ctx.send(
                _(
                    "Too many card uploads are being processed right now - "
                    "try again in a few seconds."
                )
            )
            return

        try:
            # SLOW WORK past this point (a CDN fetch, then a Pillow decode +
            # encode inside the shared image semaphore): defer first. On the
            # PREFIX path ctx.defer() is a documented no-op, so ctx.typing()
            # covers both (a real typing indicator there, an already-answered
            # defer on the slash side) - the same shape as /rank's own render.
            await ctx.defer()
            async with ctx.typing():
                try:
                    data = await background.read()
                except Exception:
                    # Deliberately broader than discord.HTTPException: the CDN
                    # read goes through aiohttp, whose transport failures are
                    # not discord exceptions and would otherwise reach the
                    # global handler as an unknown crash.
                    log.exception(
                        "Failed to download a member rank card attachment"
                    )
                    await ctx.send(
                        _("I couldn't download that attachment - try again.")
                    )
                    return

                try:
                    await self.set_user_rank_background(
                        ctx.author.id, data, background.content_type
                    )
                except rank_card.RankCardError as exc:
                    await ctx.send(rank_card_error_message(exc))
                    return
        finally:
            _INFLIGHT_UPLOADS.release()

        embed = discord.Embed(
            title=_("Rank card background updated"),
            description=_(
                "Your /rank card now uses that image, in every server that "
                "allows personal card styles."
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    @rankcard.command(name="set-accent")
    @commands.guild_only()
    @discord.app_commands.describe(
        colour="A hex colour like #5865F2 or #58F."
    )
    async def rankcard_set_accent(self, ctx: commands.Context, colour: str):
        """Set the accent colour of your rank card's progress bar.

        Servers can turn personal card styles off; where they have, this colour
        is kept but not drawn.
        """
        try:
            accent = await self.set_user_rank_accent(ctx.author.id, colour)
        except rank_card.InvalidAccent as exc:
            await ctx.send(rank_card_error_message(exc))
            return

        embed = discord.Embed(
            title=_("Rank card accent updated"),
            description=_("Your /rank card now uses {hex}.").format(
                hex="#%06X" % accent
            ),
            colour=discord.Colour(accent),
        )
        await ctx.send(embed=embed)

    @rankcard.command(name="clear")
    @commands.guild_only()
    @discord.app_commands.describe(
        part="Leave empty to clear both, or pick just the background or the accent."
    )
    async def rankcard_clear(
        self,
        ctx: commands.Context,
        part: typing.Optional[typing.Literal["background", "accent"]] = None,
    ):
        """Remove your personal rank-card style and go back to the server's."""
        await self.clear_user_rank_card(ctx.author.id, target=part)
        if part == "background":
            description = _("Your rank-card background is gone.")
        elif part == "accent":
            description = _("Your rank-card accent colour is gone.")
        else:
            description = _(
                "Your personal rank-card style is gone - your /rank cards "
                "follow each server's look again."
            )
        embed = discord.Embed(
            title=_("Rank card cleared"),
            description=description,
            colour=random_colour(),
        )
        await ctx.send(embed=embed)
