"""Level-up no-XP zones, announce control, and XP multipliers (leveling L3+L4):
the ``/levelconfig`` admin group.

Independent knobs, all consumed by cogs/community/leveling/leveling.py's on_message hot
path (and, for voice XP and boosts/events, cogs/community/leveling/voice_xp.py's sweep):

* NO-XP ZONES (``level_no_xp``): channels/categories and roles where messages
  never earn XP. This cog owns the table (add/remove/list, capped at
  ``cogs.community.leveling.engine.MAX_NO_XP_PER_GUILD``) and, after every write, calls
  ``Leveling.refresh_no_xp_snapshot`` so the change is live on the very next
  message - no restart, no reliance on cache eviction.
* ANNOUNCE CONTROL (``level_config.announce_mode`` / ``announce_channel_id`` /
  ``announce_template``, columns L1 already added): where and how a level-up is
  announced. This cog never writes those columns directly - it always goes
  through ``Leveling.set_announce_mode`` / ``set_announce_template`` (the same
  cross-cog seam cogs/config/settings.py uses for the enabled toggle), because
  the Leveling cog's ``_configs`` hot-path cache must stay in step with the DB.
* XP BOOSTS + EVENT (L4, ``xp_multipliers`` and level_config's ``event_factor``
  / ``event_ends_at``): boost or reduce XP globally, per channel/category, per
  role, or via a timed event. This cog owns both tables/columns directly (like
  level_no_xp) and, after every write, calls
  ``Leveling.refresh_multiplier_snapshot`` so the change is live on the very
  next message/sweep tick - no restart.

Cross-cog seam, matching the house pattern (cogs/community/leveling/level_rewards.py,
cogs/config/settings.py): looked up by name via ``bot.get_cog("Leveling")``,
guarded so a missing/failing Leveling cog degrades to a friendly refusal rather
than a crash - this cog owns no hot path itself.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import datetime
import logging
import typing

import discord
from discord.ext import commands

from . import engine as leveling
from . import rank_card
from tools import interactions, rendering
from tools.cooldowns import Cooldowns
from tools.formats import format_dt, random_colour
from tools.i18n import N_, _
from tools.views import AuthorLayoutView, AuthorView, LocaleModal

try:
    # The house duration converter (tools/time.py), reused elsewhere (reminders,
    # rolemenus, announcements). Preferred whenever importable; a tiny pure
    # fallback parser (cogs.community.leveling.engine.parse_short_duration) covers the
    # (never-expected-in-production) case it is not - see the event command.
    from tools.time import ShortTime
except ImportError:  # pragma: no cover - defensive only
    ShortTime = None

log = logging.getLogger(__name__)

# Localized reasons for validate_announce_template's short failure codes (that
# module has no i18n dependency, like every other tools/*.py pure decision
# engine - this cog is where the codes become user-facing text).
_TEMPLATE_ERRORS = {
    "empty": N_("The message can't be empty."),
    "malformed": N_(
        "That message has a stray '{' or '}' - check the placeholders."
    ),
    "unknown_placeholder": N_(
        "Only these placeholders are allowed: {user}, {level}, {guild}."
    ),
}


def _template_error_message(reason):
    if reason == "too_long":
        return _("The message is too long (max {max} characters).").format(
            max=leveling.MAX_ANNOUNCE_TEMPLATE_LEN
        )
    return _(_TEMPLATE_ERRORS.get(reason, _TEMPLATE_ERRORS["malformed"]))


def _describe_announce_mode(config):
    """One-line, human description of the guild's current announce_mode."""
    if config.announce_mode == "off":
        return _("Off - level-ups are never announced.")
    if config.announce_mode == "dm":
        return _("DM - the member is messaged directly.")
    if config.announce_mode == "fixed":
        if config.announce_channel_id:
            return _("Fixed channel: <#{channel_id}>").format(
                channel_id=config.announce_channel_id
            )
        return _(
            "Fixed channel (none set yet - falls back to the message's own "
            "channel)."
        )
    return _("Channel - announced where the message was sent.")


def _describe_announce_template(config):
    if config.announce_template:
        return _('Custom: "{template}"').format(template=config.announce_template)
    return _('Default: "{template}"').format(
        template=leveling.DEFAULT_ANNOUNCE_TEMPLATE
    )


def _describe_voice_xp(config):
    """One-line, human description of the guild's voice-XP setting."""
    if config.voice_xp_enabled:
        return _("On - {rate} XP per eligible minute in voice.").format(
            rate=config.voice_xp_per_minute
        )
    return _("Off - no XP for time spent in voice.")


# ----------------------------------------------------------------------
# XP multipliers (L4): boost/reduce XP globally, per channel/category, per
# role, plus a timed double-XP event.
# ----------------------------------------------------------------------

_MULTIPLIER_ERRORS = {
    "invalid": N_("The multiplier must be a plain number."),
}


def _multiplier_error_message(reason):
    if reason == "out_of_range":
        return _(
            "The multiplier must be between {min} and {max} (0 mutes XP "
            "entirely)."
        ).format(
            min=leveling.MIN_MULTIPLIER_FACTOR, max=leveling.MAX_MULTIPLIER_FACTOR
        )
    return _(_MULTIPLIER_ERRORS.get(reason, _MULTIPLIER_ERRORS["invalid"]))


def _duration_error_message(reason):
    if reason == "out_of_range":
        # MIN_EVENT_DURATION_SECONDS is a fixed 60s (1 minute) design constant
        # - see cogs/community/leveling/engine.py - so this is spelled out directly rather
        # than pluralized dynamically, matching the other bound messages in
        # this file (e.g. the voice-XP rate refusal).
        return _(
            "The event must last between 1 minute and {max_days} days (e.g. "
            "\"2h\" or \"3d\")."
        ).format(max_days=leveling.MAX_EVENT_DURATION_SECONDS // 86400)
    return _(
        "I couldn't understand that duration - try something like \"2h\" or "
        "\"3d\"."
    )


def _multiplier_lines(guild, rows):
    """(global_line_or_None, channel_lines, role_lines) rendered for the list
    card and the overview panel, resolving deleted targets to a placeholder
    rather than a broken mention (mirrors _no_xp_lines)."""
    global_line = None
    channel_lines = []
    role_lines = []
    for kind, target_id, factor in rows:
        if kind == leveling.MULTIPLIER_GLOBAL:
            global_line = _("Server-wide: **{factor}x**").format(factor=factor)
        elif kind == leveling.MULTIPLIER_CHANNEL:
            channel = guild.get_channel(target_id)
            text = (
                channel.mention
                if channel is not None
                else f"`{target_id}` " + _("(deleted)")
            )
            channel_lines.append(f"- {text}: **{factor}x**")
        else:
            role = guild.get_role(target_id)
            text = (
                role.mention if role is not None else f"`{target_id}` " + _("(deleted)")
            )
            role_lines.append(f"- {text}: **{factor}x**")
    return global_line, channel_lines, role_lines


def _describe_event(event_factor, event_ends_at):
    """One-line, human description of the guild's timed double-XP event.

    An already-expired stored row (``event_ends_at`` in the past) is
    described as "no event running" - the SAME "ignored at read time" rule
    cogs.community.leveling.engine.compute_multiplier applies, so the admin panel never
    shows a stale event as still active even in the short window before the
    next lazy-null refresh (cogs/community/leveling/leveling.py's
    refresh_multiplier_snapshot).
    """
    if (
        event_factor is None
        or event_ends_at is None
        or event_ends_at <= discord.utils.utcnow()
    ):
        return _("No XP event running. Use `/levelconfig event set` to start one.")
    return _("**{factor}x** XP until {when}.").format(
        factor=event_factor, when=format_dt(event_ends_at, "R")
    )


async def _fetch_config(pool, guild_id):
    """This guild's LevelConfig, read fresh (bypassing the enabled gate that
    resolve_config applies - an admin configuring no-xp zones or announce
    settings before ever turning leveling ON must still see/edit them)."""
    row = await pool.fetchrow(
        "SELECT enabled, cooldown_seconds, xp_min, xp_max, announce_mode, "
        "announce_channel_id, announce_template, voice_xp_enabled, "
        "voice_xp_per_minute FROM level_config WHERE guild_id = $1;",
        guild_id,
    )
    return leveling.LevelConfig.from_row(row) if row is not None else leveling.LevelConfig()


# ----------------------------------------------------------------------
# Rank card customisation (RC2): /levelconfig card
# ----------------------------------------------------------------------
# Every write below goes through the Leveling cog's RC2 seam
# (set_rank_background / set_rank_accent / clear_rank_card, see
# cogs/community/leveling/leveling.py) - this module never touches cogs.community.leveling.rank_card's
# storage functions directly, so the cache-invalidation contract that seam
# exists to enforce can never be bypassed from here.

# The upload cap as it is SPOKEN to admins, derived from the single authority
# (cogs.community.leveling.rank_card.MAX_SOURCE_BYTES) so the number in a message or in a slash
# command's help can never drift from the number the validator enforces. Binary
# megabytes floor-divided (8 * 1024 * 1024 -> 8), labelled "MB" the way every
# other user-facing string in this codebase does.
MAX_SOURCE_MB = rank_card.MAX_SOURCE_BYTES // (1024 * 1024)

# SCALE STORY: "Preview my card" runs the FULL render pipeline - a CDN avatar
# read plus a Pillow compose - inside tools.rendering's BOT-WIDE 2-slot image
# semaphore. A button has no commands.cooldown of its own, so without this a
# single admin holding the click could keep both slots busy and add latency to
# every other guild's /rank, welcome card and stats render. One preview per 5s
# per user is far above any real configuration session (the same shape as the
# music vibe card's _STATION_DEBOUNCE, and as levelconfig_card_background's own
# command cooldown). Keyed on user id only: the panel is already author-gated,
# so a per-guild key would just be the same person twice.
_PREVIEW_DEBOUNCE = Cooldowns(5.0)


def _rank_card_error_message(exc):
    """Map a typed :class:`cogs.community.leveling.rank_card.RankCardError` to a short,
    translated message - one clause per failure so a rejected upload always
    tells the admin WHY, never a bare "something went wrong"."""
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


def card_panel_state(row):
    """The panel's state dict from a :func:`cogs.community.leveling.rank_card.fetch_config` row.

    ``row`` is ``None`` for a guild that never customised its card (no
    ``rank_cards`` row at all) - both knobs then read as their stock default.
    Kept as ONE function (the house shape, cf. ``seasons_views``'
    ``season_panel_state``) so the command that opens the panel and the panel's
    own re-read cannot describe the same row differently.
    """
    if row is None:
        return {"accent": None, "has_background": False}
    return {
        "accent": row["accent"],
        "has_background": bool(row["has_background"]),
    }


async def _render_card_preview(bot, leveling_cog, guild, member):
    """Render ``member``'s rank card exactly as ``/rank`` would, using the
    guild's CURRENT customisation (not the panel's possibly-stale in-memory
    state) - the preview button's whole point is to show the real pipeline's
    output. Lives here rather than on the Leveling cog because this lot's
    leveling.py changes are scoped to the write seam alone; every piece this
    calls (ensure_rank_card_style, the render, cogs.community.leveling.rank_card.fetch_background)
    already existed for /rank, so nothing new is added there.
    """
    pool = bot.db_pool
    xp = (
        await pool.fetchval(
            "SELECT xp FROM levels WHERE guild_id = $1 AND user_id = $2;",
            guild.id,
            member.id,
        )
        or 0
    )
    level = leveling.level_for_xp(xp)
    cur_threshold = leveling.xp_for_level(level)
    next_threshold = leveling.xp_for_level(level + 1)
    rank_pos = await pool.fetchval(
        "SELECT COUNT(*) + 1 FROM levels WHERE guild_id = $1 AND xp > $2;",
        guild.id,
        xp,
    )

    avatar_bytes = await member.display_avatar.replace(size=128).read()
    accent = member.colour.to_rgb() if member.colour.value else (88, 101, 242)
    guild_accent, has_background = await leveling_cog.ensure_rank_card_style(guild.id)
    if guild_accent is not None:
        accent = guild_accent
    background = None
    if has_background:
        background = await rank_card.fetch_background(pool, guild.id)

    def _render():
        return leveling_cog._render_rank_card(
            avatar_bytes,
            member.display_name,
            level,
            rank_pos,
            xp,
            cur_threshold,
            next_threshold,
            accent,
            background,
        )

    return await rendering.run_image_job(bot, _render)


class _RankAccentModal(LocaleModal):
    """Set the guild's rank-card accent from a typed hex colour.

    Accepts any shape :func:`cogs.community.leveling.rank_card.validate_accent` does (``#RGB``,
    ``#RRGGBB``, ``0xRRGGBB``); defaults to the guild's current accent so
    submitting unchanged is a no-op write.
    """

    def __init__(self, panel):
        super().__init__(title=_("Set rank card accent"))
        self.panel = panel
        current = panel.state.get("accent")
        self.field = discord.ui.TextInput(
            label=_("Hex colour"),
            style=discord.TextStyle.short,
            required=True,
            max_length=9,
            default=("#%06X" % current) if current is not None else None,
            placeholder="#RRGGBB",
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            await self.panel.set_accent(interaction, self.field.value)
        except Exception:
            log.exception("Rank card accent modal failed")
            await interactions.notify_failure(interaction)


class _RankCardSetBackgroundButton(discord.ui.Button):
    """Points the admin at the companion attachment command - a button click
    cannot open Discord's own file picker, so this only ever shows
    instructions (the DECIDED KISS+security shape: the panel never fetches an
    arbitrary URL, only a native command Attachment parameter does)."""

    def __init__(self, panel):
        super().__init__(
            label=_("Set background..."), style=discord.ButtonStyle.primary
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.show_background_instructions(interaction)


class _RankCardResetBackgroundButton(discord.ui.Button):
    def __init__(self, panel, *, disabled):
        super().__init__(
            label=_("Reset background"),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.reset_background(interaction)


class _RankCardSetAccentButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label=_("Set accent..."), style=discord.ButtonStyle.primary)
        self.panel = panel

    async def callback(self, interaction):
        await interaction.response.send_modal(_RankAccentModal(self.panel))


class _RankCardResetAccentButton(discord.ui.Button):
    def __init__(self, panel, *, disabled):
        super().__init__(
            label=_("Reset accent"),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.reset_accent(interaction)


class _RankCardPreviewButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(
            label=_("Preview my card"), style=discord.ButtonStyle.secondary
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.preview(interaction)


class RankCardPanel(AuthorLayoutView):
    """Author-restricted admin panel for the ``rank_cards`` row (RC2).

    Single Components V2 :class:`~discord.ui.Container`, same house shape as
    :class:`~cogs.community.leveling.seasons_views.SeasonsPanel`: a header, a
    background section (state + set-instructions/reset buttons) and an accent
    section (state + set-modal/reset buttons), plus a preview button that
    renders the CLICKING admin's own card through the real pipeline and sends
    it back ephemerally. The container's own ``accent_colour`` mirrors the
    guild's configured accent when one is set - a live swatch, not just text.

    Every write calls back into the Leveling cog's RC2 seam
    (``leveling_cog.set_rank_background`` / ``set_rank_accent`` /
    ``clear_rank_card``), never ``cogs.community.leveling.rank_card`` directly, so the cache
    invalidation contract holds from this surface too.
    """

    def __init__(self, bot, leveling_cog, guild, author_id, state, *, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.bot = bot
        self.leveling_cog = leveling_cog
        self.guild = guild
        self.state = state
        self._build()

    def _build(self):
        self.clear_items()
        state = self.state
        accent_colour = (
            discord.Colour(state["accent"])
            if state["accent"] is not None
            else random_colour()
        )
        container = discord.ui.Container(accent_colour=accent_colour)

        container.add_item(
            discord.ui.TextDisplay(
                "### \N{FRAME WITH PICTURE} "
                + _("Rank card - {guild}").format(guild=self.guild.name)
                + "\n-# "
                + _(
                    "Customise the background and accent colour used on "
                    "this server's /rank cards."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        if state["has_background"]:
            background_desc = _(
                "Set - a custom {width}x{height} background image is used."
            ).format(width=rank_card.CARD_WIDTH, height=rank_card.CARD_HEIGHT)
        else:
            background_desc = _(
                "Not set - the stock panel background is used."
            )
        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Background") + "**\n" + background_desc
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _RankCardSetBackgroundButton(self),
                _RankCardResetBackgroundButton(
                    self, disabled=not state["has_background"]
                ),
            )
        )
        container.add_item(discord.ui.Separator())

        if state["accent"] is not None:
            accent_desc = _("Set - {hex}").format(hex="#%06X" % state["accent"])
        else:
            accent_desc = _(
                "Default - each member's own role colour is used."
            )
        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Accent colour") + "**\n" + accent_desc
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _RankCardSetAccentButton(self),
                _RankCardResetAccentButton(
                    self, disabled=state["accent"] is None
                ),
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_RankCardPreviewButton(self)))

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " - "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    async def _rerender(self, interaction):
        await interactions.refresh_layout(
            interaction, self.message, self, surface="rank card panel"
        )

    async def _reload_state(self):
        """Re-read the guild's row so the panel shows reality, not its memory.

        A READ, so it deliberately does NOT go through the Leveling seam (that
        exists to pair a WRITE with its cache invalidation); it hits the same
        metadata-only query :meth:`LevelConfigUI._send_card_panel` opened the
        panel with, never the blob.
        """
        row = await rank_card.fetch_config(self.bot.db_pool, self.guild.id)
        self.state = card_panel_state(row)

    async def show_background_instructions(self, interaction):
        # interactions.reply never raises (it logs an HTTP failure at debug), so
        # a click can never surface Discord's bare "This interaction failed".
        await interactions.reply(
            interaction,
            _(
                "To set a background, run `/levelconfig card background` "
                "and attach an image (PNG, JPEG, or WebP; max {mb} MB)."
            ).format(mb=MAX_SOURCE_MB),
            ephemeral=True,
        )
        # The upload lands through a SEPARATE command, so by the time the admin
        # comes back this panel's state can be stale - and a stale
        # has_background leaves "Reset background" disabled over a background
        # that really is set, a dead end. Re-read and rebuild here (the button
        # an admin naturally clicks again in that flow). Best effort: the
        # instructions are already delivered, so a failed refresh only logs.
        try:
            await self._reload_state()
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("Rank card panel refresh after background hint failed")

    async def reset_background(self, interaction):
        try:
            await self.leveling_cog.clear_rank_card(
                self.guild.id, target="background"
            )
            self.state["has_background"] = False
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("Rank card panel background reset failed")
            await interactions.notify_failure(interaction)

    async def reset_accent(self, interaction):
        try:
            await self.leveling_cog.clear_rank_card(self.guild.id, target="accent")
            self.state["accent"] = None
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("Rank card panel accent reset failed")
            await interactions.notify_failure(interaction)

    async def set_accent(self, interaction, raw_value):
        try:
            accent = await self.leveling_cog.set_rank_accent(
                self.guild.id, raw_value
            )
        except rank_card.InvalidAccent as exc:
            await interactions.notify_failure(
                interaction, _rank_card_error_message(exc)
            )
            return
        except Exception:
            log.exception("Rank card panel accent update failed")
            await interactions.notify_failure(interaction)
            return
        self.state["accent"] = accent
        self._build()
        await self._rerender(interaction)

    async def preview(self, interaction):
        # Throttle BEFORE the defer, so a refused click costs nothing but one
        # ephemeral reply - no semaphore slot, no CDN read, no Pillow work (see
        # _PREVIEW_DEBOUNCE). The window is touched only on an ALLOWED click, so
        # a burst of refused clicks can never extend it (same discipline as
        # cogs/music/views.py's _check_station_debounce).
        if _PREVIEW_DEBOUNCE.is_active(interaction.user.id):
            await interactions.reply(
                interaction,
                _("You are clicking too fast - give it a moment."),
                ephemeral=True,
            )
            return
        _PREVIEW_DEBOUNCE.touch(interaction.user.id)
        try:
            # thinking=True is load-bearing on a COMPONENT interaction: without
            # it discord.py sends deferred_message_update, which DROPS the
            # ephemeral flag and shows the clicker no loading state at all while
            # the card renders behind the image semaphore. The house pattern for
            # "a click that answers with a new ephemeral message" is
            # ephemeral+thinking (cogs/config/{rooms_config,welcome,twitch}.py).
            await interactions.defer(
                interaction,
                ephemeral=True,
                thinking=True,
                surface="rank card preview",
            )
            buf = await _render_card_preview(
                self.bot, self.leveling_cog, self.guild, interaction.user
            )
            await interaction.followup.send(
                file=discord.File(buf, filename="rank_preview.png"),
                ephemeral=True,
            )
        except Exception:
            log.exception("Rank card panel preview failed")
            await interactions.notify_failure(interaction)


# ----------------------------------------------------------------------
# Admin UX: "remove a no-xp entry" picker
# ----------------------------------------------------------------------
class _RemoveNoXpSelect(discord.ui.Select):
    """Lists every configured no-xp entry so the admin can pick one to delete.

    One option per entry; the cap (MAX_NO_XP_PER_GUILD == 50) is above
    Discord's 25-option select limit, so only the first 25 are offered here -
    an admin with more than 25 entries removes the rest in a follow-up call,
    the same soft limitation the level_rewards picker would hit past its own
    (lower) cap.
    """

    def __init__(self, cog, guild, rows):
        self.cog = cog
        self.guild = guild
        options = []
        for kind, target_id in rows[:25]:
            if kind == leveling.NO_XP_CHANNEL:
                obj = guild.get_channel(target_id)
                label = _("Channel/category")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            else:
                obj = guild.get_role(target_id)
                label = _("Role")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            options.append(
                discord.SelectOption(
                    label=f"{label}: {desc}"[:100],
                    value=f"{kind}:{target_id}",
                    description=desc[:100],
                )
            )
        super().__init__(
            placeholder=_("Pick a no-XP zone to remove..."),
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            kind, target_id_str = self.values[0].split(":")
            target_id = int(target_id_str)
            await self.cog.bot.db_pool.execute(
                "DELETE FROM level_no_xp WHERE guild_id = $1 AND kind = $2 "
                "AND target_id = $3;",
                self.guild.id,
                kind,
                target_id,
            )
            await self.cog.refresh_no_xp_cache(self.guild.id)
            await interaction.response.edit_message(
                content=_("Removed that no-XP zone."),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("No-XP zone remove select failed")
            await interaction.response.edit_message(
                content=_("Something went wrong."), view=None
            )
        finally:
            # Terminal action: the message no longer carries a view, so stop the
            # timer too (mirrors _RemoveRewardSelect - see leveling L2).
            self._owner.stop()


class _RemoveNoXpView(AuthorView):
    def __init__(self, cog, guild, author_id, rows, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        select = _RemoveNoXpSelect(cog, guild, rows)
        select._owner = self
        self.add_item(select)


# ----------------------------------------------------------------------
# Admin UX: "remove a boost" picker (L4)
# ----------------------------------------------------------------------
class _RemoveMultiplierSelect(discord.ui.Select):
    """Lists every configured boost (global/channel/role) so the admin can
    pick one to delete. One option per row; the cap
    (MAX_MULTIPLIERS_PER_GUILD == 25) keeps this within Discord's own
    25-option select limit with no truncation needed - same precedent as
    _RemoveRewardSelect."""

    def __init__(self, cog, guild, rows):
        self.cog = cog
        self.guild = guild
        options = []
        for kind, target_id, factor in rows[:25]:
            if kind == leveling.MULTIPLIER_GLOBAL:
                label = _("Global boost")
                desc = _("Server-wide")
            elif kind == leveling.MULTIPLIER_CHANNEL:
                obj = guild.get_channel(target_id)
                label = _("Channel/category boost")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            else:
                obj = guild.get_role(target_id)
                label = _("Role boost")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            options.append(
                discord.SelectOption(
                    label=f"{label} ({factor}x)"[:100],
                    value=f"{kind}:{target_id}",
                    description=desc[:100],
                )
            )
        super().__init__(
            placeholder=_("Pick an XP boost to remove..."),
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            kind, target_id_str = self.values[0].split(":")
            target_id = int(target_id_str)
            await self.cog.bot.db_pool.execute(
                "DELETE FROM xp_multipliers WHERE guild_id = $1 AND kind = $2 "
                "AND target_id = $3;",
                self.guild.id,
                kind,
                target_id,
            )
            await self.cog.refresh_multiplier_cache(self.guild.id)
            await interaction.response.edit_message(
                content=_("Removed that XP boost."),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("XP boost remove select failed")
            await interaction.response.edit_message(
                content=_("Something went wrong."), view=None
            )
        finally:
            # Terminal action: mirrors _RemoveNoXpSelect / _RemoveRewardSelect.
            self._owner.stop()


class _RemoveMultiplierView(AuthorView):
    def __init__(self, cog, guild, author_id, rows, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        select = _RemoveMultiplierSelect(cog, guild, rows)
        select._owner = self
        self.add_item(select)


# ----------------------------------------------------------------------
# CV2 cards
# ----------------------------------------------------------------------
def _no_xp_lines(guild, rows):
    """(channel_lines, role_lines) - rendered mention lines for the no-xp
    lists, resolving deleted targets to a placeholder rather than a broken
    mention."""
    channel_lines = []
    role_lines = []
    for kind, target_id in rows:
        if kind == leveling.NO_XP_CHANNEL:
            channel = guild.get_channel(target_id)
            text = channel.mention if channel is not None else f"`{target_id}` " + _(
                "(deleted)"
            )
            channel_lines.append(f"- {text}")
        else:
            role = guild.get_role(target_id)
            text = role.mention if role is not None else f"`{target_id}` " + _(
                "(deleted)"
            )
            role_lines.append(f"- {text}")
    return channel_lines, role_lines


class NoXpListView(discord.ui.LayoutView):
    """Single-page Components V2 card: every configured no-xp channel/category
    and role for this guild."""

    def __init__(self, guild, rows, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, rows)

    def _build(self, guild, rows):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("No-XP zones | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        if not rows:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "No no-XP zones configured yet. Use `/levelconfig noxp "
                        "add` to mute a channel, category, or role."
                    )
                )
            )
        else:
            channel_lines, role_lines = _no_xp_lines(guild, rows)
            if channel_lines:
                container.add_item(
                    discord.ui.TextDisplay(
                        _("**Channels & categories**\n{lines}").format(
                            lines="\n".join(channel_lines)
                        )
                    )
                )
            if role_lines:
                if channel_lines:
                    container.add_item(discord.ui.Separator())
                container.add_item(
                    discord.ui.TextDisplay(
                        _("**Roles**\n{lines}").format(lines="\n".join(role_lines))
                    )
                )
        self.add_item(container)


class MultiplierListView(discord.ui.LayoutView):
    """Single-page Components V2 card: every configured XP boost plus the
    active timed event, for this guild."""

    def __init__(
        self, guild, rows, event_factor, event_ends_at, *, timeout=180
    ):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, rows, event_factor, event_ends_at)

    def _build(self, guild, rows, event_factor, event_ends_at):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("XP boosts | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        if not rows:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "No XP boosts configured yet. Use `/levelconfig boost "
                        "add` to boost or reduce XP globally, per channel, or "
                        "per role."
                    )
                )
            )
        else:
            global_line, channel_lines, role_lines = _multiplier_lines(guild, rows)
            if global_line:
                container.add_item(discord.ui.TextDisplay(global_line))
                if channel_lines or role_lines:
                    container.add_item(discord.ui.Separator())
            if channel_lines:
                container.add_item(
                    discord.ui.TextDisplay(
                        _("**Channels & categories**\n{lines}").format(
                            lines="\n".join(channel_lines)
                        )
                    )
                )
            if role_lines:
                if channel_lines:
                    container.add_item(discord.ui.Separator())
                container.add_item(
                    discord.ui.TextDisplay(
                        _("**Roles**\n{lines}").format(lines="\n".join(role_lines))
                    )
                )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**XP event**\n{event}").format(
                    event=_describe_event(event_factor, event_ends_at)
                )
            )
        )
        self.add_item(container)


class LevelConfigOverviewView(discord.ui.LayoutView):
    """Single-page Components V2 landing card: no-xp zones, announce settings,
    voice XP, XP boosts and the active timed event."""

    def __init__(
        self,
        guild,
        rows,
        config,
        multiplier_rows,
        event_factor,
        event_ends_at,
        *,
        timeout=180,
    ):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(
            guild, rows, config, multiplier_rows, event_factor, event_ends_at
        )

    def _build(
        self, guild, rows, config, multiplier_rows, event_factor, event_ends_at
    ):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("Level config | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        channel_lines, role_lines = _no_xp_lines(guild, rows)
        no_xp_summary = (
            _("No no-XP zones configured. Use `/levelconfig noxp add`.")
            if not rows
            else "\n".join(channel_lines + role_lines)
        )
        container.add_item(
            discord.ui.TextDisplay(
                _("**No-XP zones ({count}/{max})**\n{summary}").format(
                    count=len(rows),
                    max=leveling.MAX_NO_XP_PER_GUILD,
                    summary=no_xp_summary,
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**Announce mode**\n{mode}\n**Announce message**\n{template}").format(
                    mode=_describe_announce_mode(config),
                    template=_describe_announce_template(config),
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**Voice XP**\n{voice}").format(
                    voice=_describe_voice_xp(config)
                )
            )
        )
        container.add_item(discord.ui.Separator())
        global_line, channel_boost_lines, role_boost_lines = _multiplier_lines(
            guild, multiplier_rows
        )
        boost_summary = (
            _("No XP boosts configured. Use `/levelconfig boost add`.")
            if not multiplier_rows
            else "\n".join(
                ([global_line] if global_line else [])
                + channel_boost_lines
                + role_boost_lines
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                _("**XP boosts ({count}/{max})**\n{summary}").format(
                    count=len(multiplier_rows),
                    max=leveling.MAX_MULTIPLIERS_PER_GUILD,
                    summary=boost_summary,
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**XP event**\n{event}").format(
                    event=_describe_event(event_factor, event_ends_at)
                )
            )
        )
        self.add_item(container)


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class LevelConfigUI(commands.Cog):
    """No-XP zones, level-up announce control, and XP boosts/events: the
    ``/levelconfig`` group."""

    def __init__(self, bot):
        self.bot = bot

    # -- cross-cog seam: keep the Leveling hot-path cache in step ------------
    async def refresh_no_xp_cache(self, guild_id):
        """Push the just-written level_no_xp rows into the Leveling cog's
        hot-path cache immediately (mirrors cogs/config/settings.py's
        ``bot.get_cog("Leveling").set_enabled`` call) - so the very next
        message in this guild sees the change, no restart needed. Tolerant of
        the Leveling cog not being loaded (never happens in production; keeps
        this cog testable in isolation)."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is not None:
            await leveling_cog.refresh_no_xp_snapshot(guild_id)

    async def refresh_multiplier_cache(self, guild_id):
        """The L4 sibling of refresh_no_xp_cache: pushes the just-written
        xp_multipliers rows OR level_config event columns into the Leveling
        cog's multiplier snapshot cache immediately. Called after every
        boost add/remove and every event set/off."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is not None:
            await leveling_cog.refresh_multiplier_snapshot(guild_id)

    async def _require(self, ctx, cog_name):
        """Return a sibling cog by name, or send a friendly refusal and None.

        The seam behind the folded /levelconfig rewards and /levelconfig xp
        subcommands: their bodies live in the LevelRewards / LevelAdmin cogs
        (fine-grained by concern), and each wrapper delegates to a cmd_* method
        there. Looked up by name - the same house cross-cog pattern the announce
        and voice-XP commands already use for the Leveling cog - and guarded so a
        missing sibling degrades to a refusal rather than a crash (never happens
        in production, keeps this cog testable in isolation).
        """
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
        return cog

    # -- shared reads ----------------------------------------------------
    async def _fetch_no_xp_rows(self, guild_id):
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id FROM level_no_xp WHERE guild_id = $1;",
            guild_id,
        )
        return [(row["kind"], row["target_id"]) for row in rows]

    async def _fetch_multiplier_rows(self, guild_id):
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id, factor FROM xp_multipliers "
            "WHERE guild_id = $1;",
            guild_id,
        )
        return [(row["kind"], row["target_id"], row["factor"]) for row in rows]

    async def _fetch_event(self, guild_id):
        """(event_factor, event_ends_at) for a guild, or (None, None) when no
        level_config row exists yet (an admin may configure an event before
        ever turning leveling on, same as the no-xp/announce settings)."""
        row = await self.bot.db_pool.fetchrow(
            "SELECT event_factor, event_ends_at FROM level_config "
            "WHERE guild_id = $1;",
            guild_id,
        )
        if row is None:
            return None, None
        return row["event_factor"], row["event_ends_at"]

    async def _send_overview(self, ctx):
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        config = await _fetch_config(self.bot.db_pool, ctx.guild.id)
        multiplier_rows = await self._fetch_multiplier_rows(ctx.guild.id)
        event_factor, event_ends_at = await self._fetch_event(ctx.guild.id)
        view = LevelConfigOverviewView(
            ctx.guild, rows, config, multiplier_rows, event_factor, event_ends_at
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_noxp_list(self, ctx):
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        view = NoXpListView(ctx.guild, rows)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_boost_list(self, ctx):
        rows = await self._fetch_multiplier_rows(ctx.guild.id)
        event_factor, event_ends_at = await self._fetch_event(ctx.guild.id)
        view = MultiplierListView(ctx.guild, rows, event_factor, event_ends_at)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_card_panel(self, ctx):
        leveling_cog = await self._require(ctx, "Leveling")
        if leveling_cog is None:
            return
        row = await rank_card.fetch_config(self.bot.db_pool, ctx.guild.id)
        state = card_panel_state(row)
        view = RankCardPanel(
            self.bot, leveling_cog, ctx.guild, ctx.author.id, state
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    # -- command group -----------------------------------------------------
    @commands.hybrid_group(name="levelconfig", aliases=["lvlconfig"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig(self, ctx):
        """The one leveling admin group: enable, rewards, XP, no-XP zones,
        announce, voice XP, boosts and events."""
        if ctx.invoked_subcommand is None:
            await self._send_overview(ctx)

    # -- enable toggle -------------------------------------------------
    @levelconfig.command(name="enable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(mode="True to enable, False to disable.")
    async def levelconfig_enable(self, ctx, mode: bool):
        """Enable or disable the leveling system for this server."""
        # Same L0 seam the config panel and its /config leveling predecessor
        # used: Leveling.set_enabled writes level_config AND refreshes the
        # hot-path _configs map (membership == enabled), so the toggle takes
        # effect on the very next message with no restart. The in-panel toggle
        # in cogs/config/settings.py routes through the same method, so both
        # sites stay in step.
        leveling_cog = await self._require(ctx, "Leveling")
        if leveling_cog is None:
            return
        await leveling_cog.set_enabled(ctx.guild.id, mode)
        embed = discord.Embed(
            title=_("Leveling"),
            description=(
                _("Leveling enabled for this server.")
                if mode
                else _("Leveling disabled for this server.")
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    # -- noxp subgroup -------------------------------------------------
    @levelconfig.group(name="noxp")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp(self, ctx):
        """Manage channels/categories and roles that earn no XP."""
        if ctx.invoked_subcommand is None:
            await self._send_noxp_list(ctx)

    @levelconfig_noxp.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        channel="A channel/category that should earn no XP.",
        role="A role that should earn no XP.",
    )
    async def levelconfig_noxp_add(
        self,
        ctx: commands.Context,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.CategoryChannel]
        ] = None,
        role: typing.Optional[discord.Role] = None,
    ):
        """Mute a channel/category OR a role from earning XP (give exactly one)."""
        if (channel is None) == (role is None):
            await ctx.send(
                _("Give exactly one of a channel/category or a role.")
            )
            return

        if role is not None and role.is_default():
            await ctx.send(_("You can't use @everyone as a no-XP zone."))
            return

        kind = leveling.NO_XP_CHANNEL if channel is not None else leveling.NO_XP_ROLE
        target = channel if channel is not None else role

        # Friendly fast-path refusal when the guild is already at the cap; the
        # WHERE guard inside the INSERT below is what enforces it RACE-SAFELY
        # (mirrors level_rewards_add's own atomic-cap precedent), so two admins
        # adding the 50th entry at once can never both win.
        count = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM level_no_xp WHERE guild_id = $1;", ctx.guild.id
        )
        if not leveling.can_add_no_xp_entry(count or 0):
            await ctx.send(
                _(
                    "This server already has the maximum of {max} no-XP "
                    "zones."
                ).format(max=leveling.MAX_NO_XP_PER_GUILD)
            )
            return

        inserted = await self.bot.db_pool.fetchval(
            """
            INSERT INTO level_no_xp (guild_id, kind, target_id)
            SELECT $1, $2, $3
            WHERE (SELECT COUNT(*) FROM level_no_xp WHERE guild_id = $1) < $4
            ON CONFLICT (guild_id, kind, target_id) DO NOTHING
            RETURNING kind;
            """,
            ctx.guild.id,
            kind,
            target.id,
            leveling.MAX_NO_XP_PER_GUILD,
        )
        if inserted is None:
            exists = await self.bot.db_pool.fetchval(
                "SELECT 1 FROM level_no_xp WHERE guild_id = $1 AND kind = $2 "
                "AND target_id = $3;",
                ctx.guild.id,
                kind,
                target.id,
            )
            if exists:
                await ctx.send(
                    _("{target} is already a no-XP zone.").format(
                        target=target.mention
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.send(
                    _(
                        "This server already has the maximum of {max} no-XP "
                        "zones."
                    ).format(max=leveling.MAX_NO_XP_PER_GUILD)
                )
            return

        await self.refresh_no_xp_cache(ctx.guild.id)

        embed = discord.Embed(
            title=_("No-XP zone added"),
            description=_("{target} will no longer earn XP.").format(
                target=target.mention
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelconfig_noxp.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp_remove(self, ctx):
        """Pick a no-XP zone to remove from a list of every one configured."""
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        if not rows:
            await ctx.send(_("This server has no no-XP zones configured yet."))
            return
        view = _RemoveNoXpView(self, ctx.guild, ctx.author.id, rows)
        view.message = await ctx.send(
            _("Pick a no-XP zone to remove:"), view=view
        )

    @levelconfig_noxp.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp_list(self, ctx):
        """Show every no-XP channel/category and role configured."""
        await self._send_noxp_list(ctx)

    # -- announce subgroup -----------------------------------------------
    @levelconfig.group(name="announce")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_announce(self, ctx):
        """Manage where and how level-ups are announced."""
        if ctx.invoked_subcommand is None:
            await self._send_overview(ctx)

    @levelconfig_announce.command(name="mode")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        mode="off, channel, dm, or fixed.",
        channel="The channel to announce in (required for fixed mode).",
    )
    async def levelconfig_announce_mode(
        self,
        ctx: commands.Context,
        mode: typing.Literal["off", "channel", "dm", "fixed"],
        channel: typing.Optional[discord.TextChannel] = None,
    ):
        """Set how level-ups are announced (off / channel / dm / fixed)."""
        if mode == "fixed" and channel is None:
            await ctx.send(_("Give a channel when setting the fixed mode."))
            return

        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return

        channel_id = channel.id if mode == "fixed" else None
        await leveling_cog.set_announce_mode(ctx.guild.id, mode, channel_id)

        config = leveling.LevelConfig(announce_mode=mode, announce_channel_id=channel_id)
        embed = discord.Embed(
            title=_("Announce mode updated"),
            description=_describe_announce_mode(config),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelconfig_announce.command(name="template")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        text="The message template, using {user} {level} {guild} (blank resets it)."
    )
    async def levelconfig_announce_template(
        self, ctx: commands.Context, text: typing.Optional[str] = None
    ):
        """Set a custom level-up message ({user} {level} {guild}), or reset it."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return

        if text is None or text.strip().lower() == "reset":
            await leveling_cog.set_announce_template(ctx.guild.id, None)
            await ctx.send(
                _("The level-up message was reset to the default: \"{template}\"").format(
                    template=leveling.DEFAULT_ANNOUNCE_TEMPLATE
                )
            )
            return

        stripped = text.strip()
        ok, reason = leveling.validate_announce_template(stripped)
        if not ok:
            await ctx.send(_template_error_message(reason))
            return

        await leveling_cog.set_announce_template(ctx.guild.id, stripped)
        preview = leveling.render_announce_template(
            stripped,
            user_text=ctx.author.mention,
            level=5,
            guild_name=ctx.guild.name,
        )
        embed = discord.Embed(
            title=_("Level-up message updated"),
            description=_("Preview: {preview}").format(preview=preview),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # -- voicexp subgroup ------------------------------------------------
    @levelconfig.group(name="voicexp")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_voicexp(self, ctx):
        """Manage XP earned for time spent in voice channels."""
        if ctx.invoked_subcommand is None:
            await self._send_overview(ctx)

    async def _apply_voice_xp_toggle(self, ctx, enabled):
        """Shared body of the on/off subcommands: delegate to the Leveling cog
        (so its hot-path config cache stays in step) and confirm, nudging the
        admin when leveling itself is off (voice XP grants nothing until it is
        on)."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return
        await leveling_cog.set_voice_xp_enabled(ctx.guild.id, enabled)
        if enabled:
            title = _("Voice XP enabled")
            desc = _("Members now earn XP for time spent together in voice.")
            if not leveling_cog.is_enabled(ctx.guild.id):
                desc = desc + "\n" + _(
                    "Heads up: server leveling is off, so no voice XP is "
                    "granted until you turn leveling on."
                )
        else:
            title = _("Voice XP disabled")
            desc = _("Members no longer earn XP for time in voice.")
        embed = discord.Embed(title=title, description=desc, colour=random_colour())
        await ctx.send(embed=embed)

    @levelconfig_voicexp.command(name="on")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_voicexp_on(self, ctx):
        """Turn voice XP on: members earn XP for time in voice."""
        await self._apply_voice_xp_toggle(ctx, True)

    @levelconfig_voicexp.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_voicexp_off(self, ctx):
        """Turn voice XP off."""
        await self._apply_voice_xp_toggle(ctx, False)

    @levelconfig_voicexp.command(name="rate")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(rate="XP earned per eligible minute in voice (1-60).")
    async def levelconfig_voicexp_rate(self, ctx, rate: int):
        """Set how much XP a member earns per eligible minute in voice (1-60)."""
        if not leveling.validate_voice_xp_rate(rate)[0]:
            await ctx.send(
                _(
                    "The rate must be between {min} and {max} XP per minute."
                ).format(
                    min=leveling.MIN_VOICE_XP_PER_MINUTE,
                    max=leveling.MAX_VOICE_XP_PER_MINUTE,
                )
            )
            return
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return
        await leveling_cog.set_voice_xp_rate(ctx.guild.id, rate)
        embed = discord.Embed(
            title=_("Voice XP rate updated"),
            description=_(
                "Members now earn **{rate}** XP per eligible minute in voice."
            ).format(rate=rate),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    # -- boost subgroup (L4) -----------------------------------------------
    @levelconfig.group(name="boost")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_boost(self, ctx):
        """Manage XP boosts: global, per channel/category, or per role."""
        if ctx.invoked_subcommand is None:
            await self._send_boost_list(ctx)

    @levelconfig_boost.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        factor="The XP multiplier (0-5x).",
        channel="A channel/category to boost (omit for a role or server-wide boost).",
        role="A role to boost (omit for a channel or server-wide boost).",
    )
    async def levelconfig_boost_add(
        self,
        ctx: commands.Context,
        factor: float,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.CategoryChannel]
        ] = None,
        role: typing.Optional[discord.Role] = None,
    ):
        """Boost or reduce XP (0-5x). Give a channel/category OR a role, or
        neither for a server-wide boost. Re-running this on the same target
        just updates its factor."""
        if channel is not None and role is not None:
            await ctx.send(
                _(
                    "Give at most one of a channel/category or a role - "
                    "give neither for a server-wide boost."
                )
            )
            return
        if role is not None and role.is_default():
            await ctx.send(
                _(
                    "You can't target @everyone directly - leave both the "
                    "channel and role empty for a server-wide boost instead."
                )
            )
            return

        ok, reason = leveling.validate_multiplier_factor(factor)
        if not ok:
            await ctx.send(_multiplier_error_message(reason))
            return

        if channel is not None:
            kind, target_id, target_text = (
                leveling.MULTIPLIER_CHANNEL,
                channel.id,
                channel.mention,
            )
        elif role is not None:
            kind, target_id, target_text = (
                leveling.MULTIPLIER_ROLE,
                role.id,
                role.mention,
            )
        else:
            kind = leveling.MULTIPLIER_GLOBAL
            target_id = leveling.GLOBAL_MULTIPLIER_TARGET_ID
            target_text = _("the whole server")

        # Race-safe: an existing (guild, kind, target) row always upserts its
        # factor - adjusting a boost is never blocked by the cap. The cap
        # only ever refuses a genuinely NEW row once the guild already has
        # MAX_MULTIPLIERS_PER_GUILD configured, across every kind.
        inserted = await self.bot.db_pool.fetchval(
            """
            INSERT INTO xp_multipliers (guild_id, kind, target_id, factor)
            SELECT $1, $2, $3, $4
            WHERE (SELECT COUNT(*) FROM xp_multipliers WHERE guild_id = $1) < $5
               OR EXISTS (
                   SELECT 1 FROM xp_multipliers
                   WHERE guild_id = $1 AND kind = $2 AND target_id = $3
               )
            ON CONFLICT (guild_id, kind, target_id)
                DO UPDATE SET factor = EXCLUDED.factor
            RETURNING kind;
            """,
            ctx.guild.id,
            kind,
            target_id,
            factor,
            leveling.MAX_MULTIPLIERS_PER_GUILD,
        )
        if inserted is None:
            # A concurrent add filled the last slot between the pre-check and
            # the atomic INSERT (only reachable for a genuinely new target -
            # an existing target always matches the EXISTS branch and upserts).
            await ctx.send(
                _(
                    "This server already has the maximum of {max} XP boosts."
                ).format(max=leveling.MAX_MULTIPLIERS_PER_GUILD)
            )
            return

        await self.refresh_multiplier_cache(ctx.guild.id)

        embed = discord.Embed(
            title=_("XP boost set"),
            description=_(
                "{target} now has a **{factor}x** XP multiplier."
            ).format(target=target_text, factor=factor),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelconfig_boost.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_boost_remove(self, ctx):
        """Pick an XP boost to remove from a list of every one configured."""
        rows = await self._fetch_multiplier_rows(ctx.guild.id)
        if not rows:
            await ctx.send(_("This server has no XP boosts configured yet."))
            return
        view = _RemoveMultiplierView(self, ctx.guild, ctx.author.id, rows)
        view.message = await ctx.send(_("Pick an XP boost to remove:"), view=view)

    @levelconfig_boost.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_boost_list(self, ctx):
        """Show every XP boost configured for this server."""
        await self._send_boost_list(ctx)

    # -- event subgroup (L4) -------------------------------------------------
    @levelconfig.group(name="event")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_event(self, ctx):
        """Manage the timed double-XP (or reduced-XP) event."""
        if ctx.invoked_subcommand is None:
            event_factor, event_ends_at = await self._fetch_event(ctx.guild.id)
            embed = discord.Embed(
                title=_("XP event"),
                description=_describe_event(event_factor, event_ends_at),
                colour=random_colour(),
            )
            await ctx.send(embed=embed)

    async def _write_event(self, guild_id, factor, ends_at):
        """Upsert level_config's event columns, seeding ``enabled`` from the
        legacy JSONB flag on INSERT (never touching it on UPDATE) - the same
        precedent as level_rewards_mode/set_voice_xp_enabled, so starting/
        stopping an event for a guild that enabled leveling only through the
        legacy bool never masks that flag with a fresh FALSE row. Always
        refreshes the Leveling cog's multiplier snapshot afterwards."""
        await self.bot.db_pool.execute(
            """
            INSERT INTO level_config (guild_id, enabled, event_factor, event_ends_at)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2,
                $3
            )
            ON CONFLICT (guild_id) DO UPDATE
                SET event_factor = $2, event_ends_at = $3;
            """,
            guild_id,
            factor,
            ends_at,
        )
        await self.refresh_multiplier_cache(guild_id)

    @levelconfig_event.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        factor="The XP multiplier for the event (e.g. 2 for double XP).",
        duration="How long the event runs, e.g. 2h (max 14 days).",
    )
    async def levelconfig_event_set(
        self, ctx: commands.Context, factor: float, duration: str
    ):
        """Start a timed XP event - e.g. `/levelconfig event set 2 2h`
        doubles XP for 2 hours (max 14 days)."""
        ok, reason = leveling.validate_multiplier_factor(factor)
        if not ok:
            await ctx.send(_multiplier_error_message(reason))
            return

        now = discord.utils.utcnow()
        if ShortTime is not None:
            try:
                ends_at = ShortTime(duration, now=now).dt
            except commands.BadArgument:
                await ctx.send(_duration_error_message("malformed"))
                return
            seconds = (ends_at - now).total_seconds()
        else:  # pragma: no cover - defensive only, see the module import above
            seconds = leveling.parse_short_duration(duration)
            if seconds is None:
                await ctx.send(_duration_error_message("malformed"))
                return
            ends_at = now + datetime.timedelta(seconds=seconds)

        ok, reason = leveling.validate_event_duration(seconds)
        if not ok:
            await ctx.send(_duration_error_message(reason))
            return

        await self._write_event(ctx.guild.id, factor, ends_at)

        embed = discord.Embed(
            title=_("XP event started"),
            description=_describe_event(factor, ends_at),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    @levelconfig_event.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_event_off(self, ctx):
        """Stop the active XP event (if any)."""
        await self._write_event(ctx.guild.id, None, None)
        await ctx.send(_("The XP event was stopped."))

    # -- card subgroup (RC2) -------------------------------------------------
    # The panel (RankCardPanel, above) and this command both write through the
    # Leveling cog's RC2 seam (set_rank_background / set_rank_accent /
    # clear_rank_card) - never through cogs.community.leveling.rank_card directly - so the panel
    # and the attachment command can never drift on the invalidation contract.
    @levelconfig.group(name="card")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_card(self, ctx):
        """Customise this server's rank-card background and accent colour."""
        if ctx.invoked_subcommand is None:
            await self._send_card_panel(ctx)

    # Discord forbids invoking a subcommand GROUP directly, so the group body
    # above is reachable by prefix only ("?levelconfig card") - without this,
    # the panel, the centrepiece of this surface, would have no slash form at
    # all. Same fix as /automod panel, and the same reason /levelconfig noxp
    # (etc.) carry an explicit read subcommand next to their write ones.
    @levelconfig_card.command(name="panel")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_card_panel(self, ctx):
        """Open the interactive rank-card panel for this server."""
        await self._send_card_panel(ctx)

    @levelconfig_card.command(name="background")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    # SCALE STORY: this is the only admin command that can queue an arbitrary
    # decode (up to MAX_SOURCE_PIXELS) into tools.rendering's BOT-WIDE 2-slot
    # image semaphore, so a single admin hammering it would add latency to every
    # other guild's /rank, welcome card and stats render. One upload per 10s per
    # user is far above any real configuration session and bounds that blast
    # radius (same shape as levelconfig_enable's own per-user cooldown).
    @commands.cooldown(1, 10, commands.BucketType.user)
    # A describe() string is an English literal read at DEFINITION time (it is
    # not user-locale text and never goes through _()), so interpolating the
    # module-level MAX_SOURCE_MB here still yields a plain constant str - and
    # one that cannot drift from the cap the validator enforces.
    @discord.app_commands.describe(
        background=(
            f"An image (PNG, JPEG, or WebP; max {MAX_SOURCE_MB} MB) "
            "for the rank-card background."
        )
    )
    async def levelconfig_card_background(
        self, ctx: commands.Context, background: discord.Attachment
    ):
        """Set this server's rank-card background from an attached image."""
        leveling_cog = await self._require(ctx, "Leveling")
        if leveling_cog is None:
            return

        # Cheap pre-check on the attachment's OWN declared size - refuses an
        # oversized upload before spending a round-trip downloading it (the
        # authoritative check is still validate_and_downscale's byte cap,
        # this only spares the network call on the common case).
        if background.size > rank_card.MAX_SOURCE_BYTES:
            await ctx.send(_rank_card_error_message(rank_card.SourceTooLarge()))
            return

        # SLOW WORK past this point (a network fetch, then a Pillow decode +
        # encode inside the shared image semaphore): defer first.
        await ctx.defer()

        # ...and, on the PREFIX path, ctx.defer() is a documented no-op (there
        # is no interaction to acknowledge), so a ?levelconfig card background
        # would sit silent for the whole download + decode with nothing on
        # screen. ctx.typing() covers both: a real typing indicator on the
        # prefix side, and a defer already answered (DeferTyping guards on
        # response.is_done) on the slash side. Same shape as /rank's own render.
        async with ctx.typing():
            try:
                data = await background.read()
            except Exception:
                # Deliberately broader than discord.HTTPException:
                # Attachment.read goes out over the CDN through aiohttp, whose
                # transport failures (ClientError, timeouts) are NOT discord
                # exceptions and would otherwise reach the global handler as an
                # unknown crash. Logged with the traceback, so this reports
                # rather than swallows.
                log.exception(
                    "Failed to download a rank card background attachment"
                )
                await ctx.send(
                    _("I couldn't download that attachment - try again.")
                )
                return

            try:
                await leveling_cog.set_rank_background(
                    ctx.guild.id, data, background.content_type
                )
            except rank_card.RankCardError as exc:
                await ctx.send(_rank_card_error_message(exc))
                return

        embed = discord.Embed(
            title=_("Rank card background updated"),
            description=_(
                "This server's /rank cards now use the uploaded image as "
                "their background."
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    # -- rewards subgroup (L2) ---------------------------------------------
    # Thin wrappers over the LevelRewards cog's cmd_* bodies (see
    # cogs/community/leveling/level_rewards.py). The checks and describe live here (this
    # is where the command is registered); the logic lives there.
    @levelconfig.group(name="rewards")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_rewards(self, ctx):
        """Manage level-up role rewards."""
        if ctx.invoked_subcommand is None:
            cog = await self._require(ctx, "LevelRewards")
            if cog is not None:
                await cog.cmd_list(ctx)

    @levelconfig_rewards.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        level="The level that grants the role.", role="The role to grant."
    )
    async def levelconfig_rewards_add(self, ctx, level: int, role: discord.Role):
        """Grant a role automatically when a member reaches a level."""
        cog = await self._require(ctx, "LevelRewards")
        if cog is not None:
            await cog.cmd_add(ctx, level, role)

    @levelconfig_rewards.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_rewards_remove(self, ctx):
        """Pick a level reward to remove from a list of every rule set up."""
        cog = await self._require(ctx, "LevelRewards")
        if cog is not None:
            await cog.cmd_remove(ctx)

    @levelconfig_rewards.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_rewards_list(self, ctx):
        """Show every level reward configured for this server."""
        cog = await self._require(ctx, "LevelRewards")
        if cog is not None:
            await cog.cmd_list(ctx)

    @levelconfig_rewards.command(name="mode")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        mode="stack (keep every reward) or replace (latest only)."
    )
    async def levelconfig_rewards_mode(
        self, ctx, mode: typing.Literal["stack", "replace"]
    ):
        """Set whether members keep every earned reward, or only the latest."""
        cog = await self._require(ctx, "LevelRewards")
        if cog is not None:
            await cog.cmd_mode(ctx, mode)

    # -- seasons (S2) --------------------------------------------------------
    # Thin wrapper over the Seasons cog's cmd_seasons_panel body (see
    # cogs/community/leveling/seasons.py), same delegation shape as rewards/xp above.
    @levelconfig.command(name="seasons")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_seasons(self, ctx):
        """Configure the season champion role and the season-rollover announce."""
        cog = await self._require(ctx, "Seasons")
        if cog is not None:
            await cog.cmd_seasons_panel(ctx)

    # -- xp subgroup (L5) --------------------------------------------------
    # Thin wrappers over the LevelAdmin cog's cmd_* bodies (see
    # cogs/community/leveling/level_admin.py), including the reset/resetall confirm flows.
    @levelconfig.group(name="xp")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_xp(self, ctx):
        """Admin XP tools: give, take, set, or reset a member's XP."""
        if ctx.invoked_subcommand is None:
            cog = await self._require(ctx, "LevelAdmin")
            if cog is not None:
                await cog.cmd_overview(ctx)

    @levelconfig_xp.command(name="give")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        member="The member to give XP to.",
        amount="How much XP to add (1 to 1000000).",
    )
    async def levelconfig_xp_give(self, ctx, member: discord.Member, amount: int):
        """Give a member XP (adds to their current total)."""
        cog = await self._require(ctx, "LevelAdmin")
        if cog is not None:
            await cog.cmd_give(ctx, member, amount)

    @levelconfig_xp.command(name="take")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        member="The member to take XP from.",
        amount="How much XP to remove (1 to 1000000).",
    )
    async def levelconfig_xp_take(self, ctx, member: discord.Member, amount: int):
        """Take XP from a member (floors at 0)."""
        cog = await self._require(ctx, "LevelAdmin")
        if cog is not None:
            await cog.cmd_take(ctx, member, amount)

    @levelconfig_xp.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        member="The member whose XP to set.",
        amount="The exact XP total to set (0 to 10000000).",
    )
    async def levelconfig_xp_set(self, ctx, member: discord.Member, amount: int):
        """Set a member's XP to an exact total."""
        cog = await self._require(ctx, "LevelAdmin")
        if cog is not None:
            await cog.cmd_set(ctx, member, amount)

    @levelconfig_xp.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(member="The member whose XP to reset to 0.")
    async def levelconfig_xp_reset(self, ctx, member: discord.Member):
        """Reset one member's XP to 0 (asks for confirmation first)."""
        cog = await self._require(ctx, "LevelAdmin")
        if cog is not None:
            await cog.cmd_reset(ctx, member)

    @levelconfig_xp.command(name="resetall")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_xp_resetall(self, ctx):
        """Reset EVERY member's XP for this server (double confirmation)."""
        cog = await self._require(ctx, "LevelAdmin")
        if cog is not None:
            await cog.cmd_resetall(ctx)


async def setup(bot):
    await bot.add_cog(LevelConfigUI(bot))
