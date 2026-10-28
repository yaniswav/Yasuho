"""The Suggestions cog: /suggest, /suggestions, votes, statuses and the weekly digest.

POSTING. ``/suggest`` opens a modal; the submit allocates the per-guild number,
posts the embed with the three persistent buttons, then opens a public thread on it
when Yasuho can. The row is written first (message_id 0) and discarded if the post
fails, so a failed post never leaves a numbered ghost.

VOTES. One statement toggles the member's vote (store.toggle_vote), counts come from
the votes table. The visible counts are edited through a trailing coalescer: the
first vote of a burst schedules ONE edit EDIT_DELAY seconds later, every later vote
in that window only refreshes the target, and the edit re-reads the counts, so the
last state wins and a burst costs one edit per message per window.

STATUSES. Staff are members with Manage Server or the configured staff role, checked
on the button, the select and the modal submit. rejected / implemented close voting
(buttons disabled) and archive+lock the thread; leaving them reopens both.

DIGEST (Yasuho+). One pending ``suggestion_digest`` row per enabled guild in the shared
timers table. The dispatcher deletes it before dispatch (at-most-once) and the
handler schedules the next week BEFORE posting, so a crash loses one digest and never
the series. The Yasuho+ test happens at fire time: a lapse needs no clean-up.

MENTIONS. Suggestion text is user content: every send and edit uses AllowedMentions.none().

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import json
import logging

import discord
from discord.ext import commands

from . import render, store
from .panel import SuggestionsPanel
from .views import DYNAMIC_ITEMS, ReasonModal, StatusPicker, SuggestionView, SuggestModal
from tools import i18n, premium, settings
from tools.cooldowns import Cooldowns
from tools.i18n import _

log = logging.getLogger(__name__)

EDIT_DELAY = 3.0  # seconds: one message edit per window, last state wins
SUGGEST_COOLDOWN = 60.0
NO_MENTIONS = discord.AllowedMentions.none()
_STARTUP_CONCURRENCY = 5
_DIGEST_SQL = (
    "SELECT guild_id FROM guild_settings WHERE settings @> "
    "'{\"suggestions\": {\"enabled\": true, \"digest\": true}}'::jsonb"
)


class Suggestions(commands.Cog):
    """A suggestion box with votes, statuses and a weekly digest."""

    def __init__(self, bot):
        self.bot = bot
        # Per-process pacing only (user-keyed, self-sweeping, nothing to purge).
        self._cd = Cooldowns(SUGGEST_COOLDOWN)
        # suggestion id -> {"guild", "message"}: the vote edits waiting for their window.
        self._pending = {}
        self._tasks = set()

    async def cog_load(self):
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)

    def cog_unload(self):
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)
        for task in list(self._tasks):
            task.cancel()

    # -- helpers ------------------------------------------------------------

    async def config_for(self, guild_id):
        return store.normalise(
            await settings.get_guild(self.bot.db_pool, guild_id, store.SETTINGS_KEY, None)
        )

    @staticmethod
    def is_staff(member, config):
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.manage_guild:
            return True
        role_id = config["staff_role_id"]
        # Never @everyone (a dashboard-written blob could hold the guild id).
        return (
            bool(role_id)
            and role_id != getattr(getattr(member, "guild", None), "id", None)
            and any(r.id == role_id for r in getattr(member, "roles", ()))
        )

    def _custom_allowed(self, guild_id):
        return premium.resolve_guild_limits(self.bot, guild_id).suggestions_custom

    def allowed_statuses(self, guild_id, config):
        keys = list(store.BUILTIN_STATUSES)
        if self._custom_allowed(guild_id):
            keys += [store.custom_key(c["label"]) for c in config["custom_statuses"]]
        return keys

    # -- commands -----------------------------------------------------------

    @commands.hybrid_command(name="suggest")
    @commands.guild_only()
    async def suggest(self, ctx):
        """Post a suggestion for this server."""
        if ctx.interaction is None:
            return await ctx.send(_("Use the /suggest slash command to write a suggestion."))
        config = await self.config_for(ctx.guild.id)
        if not config["enabled"] or not config["channel_id"]:
            return await ctx.send(
                _("The suggestion box is not set up in this server."), ephemeral=True
            )
        if self._cd.is_active((ctx.guild.id, ctx.author.id)):
            return await ctx.send(
                _("Please wait a minute between suggestions."), ephemeral=True
            )
        await ctx.interaction.response.send_modal(SuggestModal(self))

    @commands.hybrid_command(name="suggestions")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def suggestions(self, ctx):
        """Set up the suggestion box."""
        config = await self.config_for(ctx.guild.id)
        view = SuggestionsPanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(embed=view.build_embed(), view=view, ephemeral=True)

    # -- posting ------------------------------------------------------------

    async def submit(self, interaction, title, details):
        await interaction.response.defer(ephemeral=True)
        guild, member = interaction.guild, interaction.user
        if guild is None or not title or not details:
            return await interaction.followup.send(
                _("A suggestion needs a title and details."), ephemeral=True
            )
        config = await self.config_for(guild.id)
        channel = guild.get_channel(config["channel_id"] or 0)
        if not config["enabled"] or channel is None:
            return await interaction.followup.send(
                _("The suggestion box is not set up in this server."), ephemeral=True
            )
        key = (guild.id, member.id)
        if self._cd.is_active(key):
            return await interaction.followup.send(
                _("Please wait a minute between suggestions."), ephemeral=True
            )
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.followup.send(
                _("I can't post in the suggestions channel. Ask a moderator to check my permissions."),
                ephemeral=True,
            )
        self._cd.touch(key)
        row = await store.create(self.bot.db_pool, guild.id, channel.id, member.id, title, details)
        try:
            message = await self._post(guild, channel, perms, row, member, config)
        except Exception:
            log.exception("SUGGEST-FAIL guild=%s reason=post", guild.id)
            await store.discard(self.bot.db_pool, row["id"])
            return await interaction.followup.send(
                _("I couldn't post your suggestion. Please try again later."), ephemeral=True
            )
        await interaction.followup.send(
            _("Your suggestion was posted: {url}").format(url=message.jump_url), ephemeral=True
        )

    async def _post(self, guild, channel, perms, row, member, config):
        locale = await i18n.resolve_guild_locale(self.bot, guild)
        with i18n.locale(locale):
            embed = render.build_embed(
                row, 0, 0, member.display_name, member.display_avatar.url, config
            )
            view = SuggestionView(row["id"])
        message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        thread_id = None
        if perms.create_public_threads:
            try:
                thread = await message.create_thread(
                    name="#{n} {title}".format(n=row["number"], title=row["title"])[:100],
                    auto_archive_duration=10080,
                )
                thread_id = thread.id
            except discord.HTTPException:
                thread_id = None  # no thread is not an error
        try:
            if not await store.attach_message(self.bot.db_pool, row["id"], message.id, thread_id):
                raise LookupError("suggestion erased while posting")
        except Exception:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            raise
        return message

    # -- votes --------------------------------------------------------------

    async def vote(self, interaction, suggestion_id, value):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return
        outcome = await store.toggle_vote(
            self.bot.db_pool, suggestion_id, guild.id, interaction.user.id, value
        )
        if outcome in ("cast", "removed"):
            self._request_refresh(suggestion_id, guild, interaction.message)
        text = {
            "cast": _("Your vote was recorded."),
            "removed": _("Your vote was removed."),
            "closed": _("Voting is closed on this suggestion."),
            "missing": _("This suggestion no longer exists."),
        }[outcome]
        await interaction.followup.send(text, ephemeral=True)

    def _request_refresh(self, suggestion_id, guild, message):
        entry = self._pending.get(suggestion_id)
        if entry is not None:
            entry["message"] = message
            return
        self._pending[suggestion_id] = {"guild": guild, "message": message}
        task = asyncio.get_running_loop().create_task(self._flush(suggestion_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush(self, suggestion_id):
        await asyncio.sleep(EDIT_DELAY)
        entry = self._pending.pop(suggestion_id, None)
        if entry is None:
            return
        try:
            await self.refresh_message(entry["guild"], suggestion_id, entry["message"])
        except Exception:
            log.warning("SUGGEST-FAIL guild=%s reason=refresh", entry["guild"].id, exc_info=True)

    async def refresh_message(self, guild, suggestion_id, message=None):
        """Re-render the posted message from the database (counts, status, buttons)."""
        row = await store.get(self.bot.db_pool, suggestion_id, guild.id)
        if row is None or not row["message_id"]:
            return
        up, down = await store.counts(self.bot.db_pool, suggestion_id)
        config = await self.config_for(guild.id)
        if message is None:
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                return
            try:
                message = await channel.fetch_message(row["message_id"])
            except discord.HTTPException:
                log.info("SUGGEST-SKIP guild=%s reason=fetch", guild.id)
                return
        name, icon = render.embed_author(message)
        locale = await i18n.resolve_guild_locale(self.bot, guild)
        with i18n.locale(locale):
            embed = render.build_embed(row, up, down, name, icon, config)
            view = SuggestionView(suggestion_id, up, down, store.is_closing(row["status"]))
        try:
            await message.edit(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            log.info("SUGGEST-SKIP guild=%s reason=edit", guild.id)

    # -- statuses -----------------------------------------------------------

    async def _refuse_staff(self, interaction):
        await interaction.response.send_message(
            _("Only the server's staff can change a suggestion's status."), ephemeral=True
        )

    async def open_status_menu(self, interaction, suggestion_id):
        guild = interaction.guild
        config = await self.config_for(guild.id)
        if not self.is_staff(interaction.user, config):
            return await self._refuse_staff(interaction)
        picker = StatusPicker(
            self, interaction.user.id, suggestion_id, self.allowed_statuses(guild.id, config)
        )
        await interaction.response.send_message(
            _("Choose the new status."), view=picker, ephemeral=True
        )

    async def pick_status(self, interaction, suggestion_id, status):
        config = await self.config_for(interaction.guild.id)
        if not self.is_staff(interaction.user, config):
            return await self._refuse_staff(interaction)
        if status not in self.allowed_statuses(interaction.guild.id, config):
            return await interaction.response.send_message(
                _("That status is not available."), ephemeral=True
            )
        await interaction.response.send_modal(ReasonModal(self, suggestion_id, status))

    async def apply_status(self, interaction, suggestion_id, status, reason):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.config_for(guild.id)
        if not self.is_staff(interaction.user, config):
            return await interaction.followup.send(
                _("Only the server's staff can change a suggestion's status."), ephemeral=True
            )
        if status not in self.allowed_statuses(guild.id, config):
            return await interaction.followup.send(_("That status is not available."), ephemeral=True)
        row = await store.set_status(self.bot.db_pool, suggestion_id, guild.id, status, reason)
        if row is None:
            return await interaction.followup.send(
                _("This suggestion no longer exists."), ephemeral=True
            )
        await self.refresh_message(guild, suggestion_id)
        await self._sync_thread(guild, row)
        await interaction.followup.send(
            _("Status set to {status}.").format(status=render.status_label(status)),
            ephemeral=True,
        )

    async def _sync_thread(self, guild, row):
        """Archive+lock the thread when voting closes, reopen it when it comes back."""
        closing = store.is_closing(row["status"])
        if not row["thread_id"] or closing == store.is_closing(row["previous"]):
            return
        try:
            thread = guild.get_thread(row["thread_id"]) or await self.bot.fetch_channel(
                row["thread_id"]
            )
            if closing and thread.archived:
                # Discord refuses edits on an archived thread: unarchive first.
                await thread.edit(archived=False, locked=True)
            await thread.edit(archived=closing, locked=closing)
        except discord.HTTPException:
            log.info("SUGGEST-SKIP guild=%s reason=thread", guild.id)

    # -- digest ---------------------------------------------------------------

    async def _write_timer(self, guild_id, config, *, replace):
        """Keep one pending digest row: ``replace`` moves it to the current config.

        Returns the planned time of the next digest, or None when there is none.
        """
        now = discord.utils.utcnow()
        wanted = config["enabled"] and config["channel_id"] and config["digest"]
        planned = store.next_digest(now, config["digest_weekday"]) if wanted else None
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1, $2)", store.LOCK_NS, guild_id % 2147483647
                )
                if replace or not wanted:
                    # A row already due stays: the dispatcher still owes that digest.
                    await conn.execute(
                        "DELETE FROM timers WHERE event = $1 AND claimed_at IS NULL "
                        "AND extra->>'guild_id' = $2 AND expires > $3",
                        store.TIMER_EVENT, str(guild_id), now,
                    )
                if wanted:
                    await conn.execute(
                        "INSERT INTO timers (event, expires, extra) "
                        "SELECT $1::text, $2, $3::jsonb WHERE NOT EXISTS ("
                        "SELECT 1 FROM timers WHERE event = $1 AND extra->>'guild_id' = $4)",
                        store.TIMER_EVENT,
                        planned,
                        json.dumps({"guild_id": guild_id}),
                        str(guild_id),
                    )
        reminder = self.bot.get_cog("Reminder")
        if reminder is not None:
            reminder._have_data.set()
        return planned

    async def reconcile_digest(self, guild_id):
        """After a panel save: move the pending row to the new config."""
        return await self._write_timer(guild_id, await self.config_for(guild_id), replace=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Every READY: make sure each digest guild has its pending row (idempotent)."""
        try:
            rows = await self.bot.db_pool.fetch(_DIGEST_SQL)
            gate = asyncio.Semaphore(_STARTUP_CONCURRENCY)

            async def one(guild_id):
                async with gate:
                    try:
                        await self._write_timer(
                            guild_id, await self.config_for(guild_id), replace=False
                        )
                    except Exception:
                        log.exception("SUGGEST-FAIL guild=%s reason=reconcile", guild_id)

            await asyncio.gather(
                *(one(r["guild_id"]) for r in rows if self.bot.get_guild(r["guild_id"]))
            )
        except Exception:
            log.exception("Suggestion digest startup reconcile failed")

    @commands.Cog.listener()
    async def on_suggestion_digest_timer_complete(self, extra):
        try:
            reason = await self._digest(extra)
        except discord.HTTPException as exc:
            reason = "http_" + type(exc).__name__.lower()
        except Exception:
            log.exception("Suggestion digest crashed")
            reason = "error"
        gid = extra.get("guild_id") if isinstance(extra, dict) else None
        if reason == "sent":
            log.info("SUGGEST-DIGEST-SENT guild=%s", gid)
        elif reason.startswith("skip_"):
            log.info("SUGGEST-DIGEST-SKIP guild=%s reason=%s", gid, reason[5:])
        else:
            log.warning("SUGGEST-DIGEST-FAIL guild=%s reason=%s", gid, reason)

    async def _digest(self, extra):
        """Return ``sent``, ``skip_<why>`` or a failure code."""
        try:
            guild_id = int(extra["guild_id"])
        except (KeyError, TypeError, ValueError):
            return "bad_row"
        config = await self.config_for(guild_id)
        if not (config["enabled"] and config["channel_id"] and config["digest"]):
            return "skip_disabled"
        # Next week first: a crash below costs this digest, never the series.
        await self._write_timer(guild_id, config, replace=False)
        if not self._custom_allowed(guild_id):
            return "skip_not_plus"
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.me is None:
            return "guild_unavailable"
        channel = guild.get_channel(config["channel_id"])
        if channel is None:
            return "channel_missing"
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return "missing_permissions"
        rows = await store.top_recent(self.bot.db_pool, guild_id)
        if not rows:
            return "skip_empty"
        locale = await i18n.resolve_guild_locale(self.bot, guild)
        with i18n.locale(locale):
            embed = self._digest_embed(guild_id, rows)
        await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        return "sent"

    @staticmethod
    def _digest_embed(guild_id, rows):
        lines = [
            "**#{n}** {title} ({score:+d})\nhttps://discord.com/channels/{g}/{c}/{m}".format(
                n=r["number"],
                title=discord.utils.escape_markdown(discord.utils.escape_mentions(r["title"])),
                score=r["score"], g=guild_id, c=r["channel_id"], m=r["message_id"],
            )
            for r in rows
        ]
        return discord.Embed(
            title=_("Top suggestions this week"),
            description="\n\n".join(lines)[:4096],
            colour=store.DEFAULT_COLOUR,
        )
