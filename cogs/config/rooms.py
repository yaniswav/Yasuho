"""Join-to-create voice hubs ("autorooms").

A guild can run up to five join-to-create voice hubs, one per game mode
(Ranked, Quickplay, Arcade, ...). Each hub owns a category, a trigger voice
channel and a room-name template. When a member joins a hub's trigger channel
we spin up a fresh temp room from the template, move them in, and delete the
room once it empties.

Configuration lives in the ``guild_settings`` JSONB (via ``tools/settings.py``)
under the ``autorooms`` key as a list of hub dicts. All shaping/validation is
delegated to the pure ``tools/autoroom.py`` helpers; this cog only performs the
Discord and DB side effects. On first load, any rows from the legacy
``auto_room`` table are migrated into default hubs and then ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

import discord
from discord.ext import commands

# The autoroom UI (hub-config panel and per-room voicemaster panel) lives in
# two sibling modules after a move-only split. They are re-imported here so that
# every existing ``cogs.config.rooms.<name>`` import keeps resolving. The cog
# instantiates ``AutoroomPanel`` and ``RoomControlView`` at runtime; the rest
# are pure re-exports flagged for the linter.
from .rooms_config import (
    DEFAULT_CATEGORY_NAME,  # noqa: F401
    DEFAULT_HUB_NAME,  # noqa: F401
    AddHubModal,  # noqa: F401
    AutoroomPanel,
    EditHubModal,  # noqa: F401
    _HubManageView,  # noqa: F401
    _make_limit_select,  # noqa: F401
    _parse_int,  # noqa: F401
    _RemoveSelect,  # noqa: F401
    _RenameChannelsModal,  # noqa: F401
)
from .rooms_panels import (
    _ACTION_BUMP,  # noqa: F401
    _ACTION_CLAIM,  # noqa: F401
    _ACTION_HIDE,  # noqa: F401
    _ACTION_KICK,  # noqa: F401
    _ACTION_LIMIT,  # noqa: F401
    _ACTION_LOCK,  # noqa: F401
    _ACTION_RENAME,  # noqa: F401
    _ACTION_TRANSFER,  # noqa: F401
    _ACTION_UNBLACKLIST,  # noqa: F401
    _CLAIM_CUSTOM_ID,  # noqa: F401
    _MEMBER_OPTION_CAP,  # noqa: F401
    RoomControlView,
    _MemberActionSelect,  # noqa: F401
    _PanelButton,  # noqa: F401
    _RoomRenameModal,  # noqa: F401
    _RoomSubView,  # noqa: F401
    _SlotSelect,  # noqa: F401
)
from tools import settings
from tools.autoroom import (
    CREATE_COOLDOWN_SECONDS,
    DEFAULT_LABEL,
    GUILD_CHANNEL_BUDGET,
    HUB_OVERHEAD_CHANNELS,
    MAX_CATEGORIES,
    MAX_HUBS,
    can_add_hub,
    default_hub,
    normalize_hubs,
    owner_from_overwrites,
    render_room_name,
    summarise_hub,
)
from tools.cooldowns import Cooldowns
from tools.i18n import _

log = logging.getLogger(__name__)

# How often the empty-room reaper wakes to check a tracked room.
CLEANUP_INTERVAL_SECONDS = 15


class TemporaryRooms(commands.Cog):
    """Create and clean up temporary voice rooms from join-to-create hubs."""

    def __init__(self, bot):
        self.bot = bot
        # {guild_id: {hub_channel_id: hub}} - the fast negative-cached lookup
        # the voice event consults. A guild absent here has no hubs, so
        # unconfigured guilds cost zero work on every voice update.
        self._hub_index = {}
        # {(guild_id, hub_id): set(channel_id)} temp rooms alive per hub.
        self._active = defaultdict(set)
        # Per-(guild, user) anti-spam debounce that prunes itself (was an
        # unbounded dict of last-created timestamps).
        self._cooldowns = Cooldowns(CREATE_COOLDOWN_SECONDS)
        self._locks = defaultdict(asyncio.Lock)  # per-guild creation lock
        self._cleanup_tasks = set()  # outstanding empty-room reapers
        # {channel_id: owner_user_id} - a fast CACHE of room ownership. The
        # source of truth is the owner's manage_channels overwrite ON the
        # channel, so a restart re-seeds this map from the channels themselves
        # (see the on_ready re-adoption). Everything else (limit, name, lock,
        # hide, blacklist) is likewise channel-backed and survives a restart.
        # Dropped when the reaper deletes the room.
        self._room_owners = {}
        # {channel_id: RoomControlView} so the timeout=None panels can be
        # stopped when their room is reaped.
        self._room_views = {}
        # Guard so the once-per-process startup re-adoption (which survives the
        # restart) runs a single time even though on_ready can fire repeatedly.
        self._adopted = False

    # ------------------------------------------------------------------
    # Load / migration
    # ------------------------------------------------------------------

    async def cog_load(self):
        """Seed the hub index from settings, migrating legacy rows once."""
        self._hub_index = {}
        configured = set()
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT guild_id, settings FROM guild_settings"
            )
        except Exception:
            log.exception("Failed to load autoroom settings")
            rows = []
        for row in rows:
            raw = row["settings"]
            try:
                data = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (TypeError, ValueError):
                continue
            if "autorooms" not in data:
                continue
            configured.add(int(row["guild_id"]))
            hubs = normalize_hubs(data.get("autorooms"))
            if hubs:
                self._index_guild(int(row["guild_id"]), hubs)
        await self._migrate_legacy(configured)

    async def _migrate_legacy(self, configured):
        """Fold old ``auto_room`` rows into default hubs (best-effort, once)."""
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT guild_id, channel_id FROM auto_room"
            )
        except Exception:
            return
        by_guild = defaultdict(list)
        for row in rows:
            by_guild[int(row["guild_id"])].append(int(row["channel_id"]))
        for guild_id, channel_ids in by_guild.items():
            if guild_id in configured:
                continue  # already migrated / configured on the JSONB store
            hubs = []
            for channel_id in channel_ids:
                if not can_add_hub(hubs):
                    break
                channel = self.bot.get_channel(channel_id)
                category = getattr(channel, "category", None)
                hubs.append(
                    default_hub(
                        hub_channel_id=channel_id,
                        category_id=category.id if category else None,
                    )
                )
            if not hubs:
                continue
            try:
                await self._save_hubs(guild_id, hubs)
            except Exception:
                log.exception("Failed to migrate legacy autorooms for %s", guild_id)

    def cog_unload(self):
        """Cancel outstanding reapers and stop any live room control panels."""
        for task in list(self._cleanup_tasks):
            task.cancel()
        for view in list(self._room_views.values()):
            view.stop()
        self._room_views.clear()
        self._room_owners.clear()

    def _forget_room(self, channel_id):
        """Drop a temp room's in-memory state and stop its control panel."""
        self._room_owners.pop(channel_id, None)
        view = self._room_views.pop(channel_id, None)
        if view is not None:
            view.stop()

    async def _grant_room_owner(self, channel, member):
        """Grant ``member`` owner-level perms on a temp room (best-effort).

        The ``manage_channels`` grant is doubly meaningful: it hands the member
        real control of their own room AND marks ownership on the channel itself,
        which is the source of truth that lets ownership survive a restart.
        """
        try:
            overwrite = channel.overwrites_for(member)
            overwrite.manage_channels = True
            overwrite.move_members = True
            overwrite.connect = True
            await channel.set_permissions(member, overwrite=overwrite)
        except discord.HTTPException:
            log.debug("Could not grant room owner perms", exc_info=True)

    async def _revoke_room_owner(self, channel, member):
        """Strip the ownership marker from ``member`` on a temp room (best-effort).

        Clears the ``manage_channels``/``move_members`` grant so the channel no
        longer reports ``member`` as its owner; ``connect`` is left untouched.
        """
        try:
            overwrite = channel.overwrites_for(member)
            overwrite.manage_channels = None
            overwrite.move_members = None
            await channel.set_permissions(member, overwrite=overwrite)
        except discord.HTTPException:
            log.debug("Could not revoke room owner perms", exc_info=True)

    def _owner_from_channel(self, channel):
        """Read the owner id off a room's channel overwrites, or ``None``.

        The channel-backed source of truth: whichever member holds an explicit
        ``manage_channels`` grant owns the room. Roles are ignored - only member
        overwrites can mark ownership.
        """
        pairs = [
            (target.id, overwrite.manage_channels)
            for target, overwrite in channel.overwrites.items()
            if isinstance(target, discord.Member)
        ]
        return owner_from_overwrites(pairs)

    def _owner_of(self, channel_id):
        """Resolve a room's owner: cache first, then the channel overwrite.

        The in-memory map is only a cache; the channel's ``manage_channels``
        overwrite is authoritative. When the cache misses (e.g. right after a
        restart) we read the channel and re-seed the cache so later lookups are
        cheap. Returns ``None`` when the room has no owner.
        """
        owner_id = self._room_owners.get(channel_id)
        if owner_id is not None:
            return owner_id
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.VoiceChannel):
            owner_id = self._owner_from_channel(channel)
            if owner_id is not None:
                self._room_owners[channel_id] = owner_id
        return owner_id

    async def _transfer_room(self, channel, member):
        """Move room ownership to ``member`` on both the channel and the cache.

        Revokes the previous owner's ``manage_channels`` marker (so exactly one
        member owns the channel), grants it to ``member`` and updates the cache.
        """
        prev_id = self._owner_of(channel.id)
        if prev_id is not None and prev_id != member.id:
            prev = channel.guild.get_member(prev_id)
            if prev is not None:
                await self._revoke_room_owner(channel, prev)
        self._room_owners[channel.id] = member.id
        await self._grant_room_owner(channel, member)

    async def _post_room_controls(self, channel, owner):
        """Post the per-room voicemaster control panel in the room's chat.

        A Components V2 ``LayoutView`` is sent with ``view=`` only (no embed, no
        content); the owner is read from ``_room_owners`` when the view builds
        its header, so ``owner`` is only needed to ensure the map is populated
        before the panel renders (the room-creation path already sets it).
        """
        try:
            view = RoomControlView(self, channel.id)
            message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = message
            self._room_views[channel.id] = view
        except discord.HTTPException:
            log.debug("Could not post room control panel", exc_info=True)

    # ------------------------------------------------------------------
    # Config helpers (settings <-> index)
    # ------------------------------------------------------------------

    def _index_guild(self, guild_id, hubs):
        """Point the in-memory index at ``hubs`` for one guild."""
        mapping = {hub["hub_channel_id"]: hub for hub in hubs if hub.get("hub_channel_id")}
        if mapping:
            self._hub_index[guild_id] = mapping
        else:
            self._hub_index.pop(guild_id, None)

    async def _load_hubs(self, guild_id):
        """Return the guild's normalised hub list from settings."""
        blob = await settings.get_guild(self.bot.db_pool, guild_id, "autorooms", [])
        return normalize_hubs(blob)

    async def _save_hubs(self, guild_id, hubs):
        """Persist ``hubs`` to settings and refresh the in-memory index."""
        hubs = normalize_hubs(hubs)
        await settings.set_guild(self.bot.db_pool, guild_id, "autorooms", hubs)
        self._index_guild(guild_id, hubs)
        return hubs

    # ------------------------------------------------------------------
    # Add / edit / remove (called from the panel/modals)
    # ------------------------------------------------------------------

    async def _add_hub(self, guild, *, label, category_name, hub_name, template, user_limit):
        """Create the hub's category + trigger channel, then save the config."""
        hubs = await self._load_hubs(guild.id)
        if not can_add_hub(hubs):
            return _("You already have the maximum of {max_hubs} hubs.").format(
                max_hubs=MAX_HUBS
            )
        if len(guild.categories) >= MAX_CATEGORIES:
            return _(
                "This server is at Discord's limit of {max} categories."
            ).format(max=MAX_CATEGORIES)
        if len(guild.channels) + HUB_OVERHEAD_CHANNELS > GUILD_CHANNEL_BUDGET:
            return _(
                "This server is too close to Discord's {budget}-channel limit "
                "to add another hub."
            ).format(budget=GUILD_CHANNEL_BUDGET)

        try:
            category = await guild.create_category(category_name[:100])
            hub_channel = await guild.create_voice_channel(
                hub_name[:100], category=category
            )
        except discord.HTTPException:
            log.exception("Failed to create autoroom channels")
            return _("Something went wrong while creating the hub's channels.")

        hub = default_hub(
            label=label,
            category_id=category.id,
            hub_channel_id=hub_channel.id,
            template=template,
            user_limit=user_limit,
        )
        hubs.append(hub)
        await self._save_hubs(guild.id, hubs)
        return _("Created the **{label}** hub. Members can join {channel} now.").format(
            label=hub["label"], channel=hub_channel.mention
        )

    async def _edit_hub(
        self, guild, hub_id, *, label, template, user_limit, max_rooms, private
    ):
        """Update the editable fields of an existing hub."""
        hubs = await self._load_hubs(guild.id)
        hub = next((h for h in hubs if h["id"] == hub_id), None)
        if hub is None:
            return _("That hub no longer exists.")
        hub.update(
            label=label,
            template=template,
            user_limit=user_limit,
            max_rooms=max_rooms,
            private=private,
        )
        await self._save_hubs(guild.id, hubs)
        return _("Updated the **{label}** hub.").format(label=hub["label"])

    async def _remove_hub(self, guild, hub_id):
        """Delete a hub's category + trigger channel and drop its config."""
        hubs = await self._load_hubs(guild.id)
        hub = next((h for h in hubs if h["id"] == hub_id), None)
        if hub is None:
            return _("That hub no longer exists.")

        # Delete the trigger channel, then the category and everything left in it
        # (its live temp rooms). Best-effort: config removal must still proceed.
        category = guild.get_channel(hub["category_id"]) if hub.get("category_id") else None
        hub_channel = (
            guild.get_channel(hub["hub_channel_id"]) if hub.get("hub_channel_id") else None
        )
        if hub_channel is not None:
            try:
                await hub_channel.delete()
            except discord.HTTPException:
                log.debug("Could not delete hub channel", exc_info=True)
        if isinstance(category, discord.CategoryChannel):
            for child in list(category.channels):
                self._forget_room(child.id)
                try:
                    await child.delete()
                except discord.HTTPException:
                    log.debug("Could not delete hub category child", exc_info=True)
            try:
                await category.delete()
            except discord.HTTPException:
                log.debug("Could not delete hub category", exc_info=True)

        self._active.pop((guild.id, hub_id), None)
        remaining = [h for h in hubs if h["id"] != hub_id]
        await self._save_hubs(guild.id, remaining)
        return _("Removed the **{label}** hub.").format(
            label=hub.get("label") or DEFAULT_LABEL
        )

    async def _rename_hub_channels(self, guild, hub_id, *, category_name, hub_name):
        """Rename a hub's category and join-to-create channel (best-effort).

        Only the Discord channels are touched; the hub's stored config is
        unchanged. Blank fields are skipped, and a missing channel is simply
        reported rather than raising.
        """
        hubs = await self._load_hubs(guild.id)
        hub = next((h for h in hubs if h["id"] == hub_id), None)
        if hub is None:
            return _("That hub no longer exists.")

        category = (
            guild.get_channel(hub["category_id"]) if hub.get("category_id") else None
        )
        hub_channel = (
            guild.get_channel(hub["hub_channel_id"])
            if hub.get("hub_channel_id")
            else None
        )
        renamed = []
        if category_name and isinstance(category, discord.CategoryChannel):
            try:
                await category.edit(name=category_name[:100])
                renamed.append(_("category"))
            except discord.HTTPException:
                log.debug("Could not rename hub category", exc_info=True)
        if hub_name and isinstance(hub_channel, discord.VoiceChannel):
            try:
                await hub_channel.edit(name=hub_name[:100])
                renamed.append(_("join-to-create channel"))
            except discord.HTTPException:
                log.debug("Could not rename hub channel", exc_info=True)

        if not renamed:
            return _("Nothing was renamed - those channels are missing.")
        return _("Renamed the {targets}.").format(targets=" & ".join(renamed))

    # ------------------------------------------------------------------
    # Room creation + cleanup
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not after.channel:
            return
        hubs = self._hub_index.get(member.guild.id)
        if not hubs:
            return
        hub = hubs.get(after.channel.id)
        if hub is None:
            return

        # Per-user cooldown to kill join/leave spam.
        key = (member.guild.id, member.id)
        if self._cooldowns.is_active(key):
            return
        self._cooldowns.touch(key)

        async with self._locks[member.guild.id]:
            await self._create_room(member, after.channel, hub)

    async def _create_room(self, member, hub_channel, hub):
        """Provision one temp room for ``member`` from ``hub``'s template."""
        try:
            guild = member.guild
            category = None
            if hub.get("category_id"):
                category = guild.get_channel(hub["category_id"])
            if not isinstance(category, discord.CategoryChannel):
                category = hub_channel.category
            if category is None:
                return

            # Enforce the hub's max_rooms cap, pruning any rooms already gone.
            active = {
                cid
                for cid in self._active[(guild.id, hub["id"])]
                if self.bot.get_channel(cid) is not None
            }
            self._active[(guild.id, hub["id"])] = active
            if len(active) >= hub["max_rooms"]:
                return  # at capacity: refuse quietly

            name = render_room_name(
                hub["template"], member.display_name, len(active) + 1
            )
            kwargs = {"category": category}
            if hub["user_limit"] > 0:
                kwargs["user_limit"] = hub["user_limit"]
            if hub["private"]:
                # Actually lock the room: deny @everyone connect (the old code
                # only granted the creator manage perms, which _grant_room_owner
                # does anyway, so "private" rooms were joinable by all). The owner
                # keeps connect + control and can admit people via the panel.
                kwargs["overwrites"] = {
                    guild.default_role: discord.PermissionOverwrite(connect=False),
                    member: discord.PermissionOverwrite(
                        connect=True, manage_channels=True, move_members=True
                    ),
                }

            new_channel = await guild.create_voice_channel(name, **kwargs)
            active.add(new_channel.id)
            # Cache the owner in memory (fast path) AND stamp a manage_channels
            # overwrite on the channel itself: that overwrite is the source of
            # truth, so ownership survives a restart that wipes this map.
            self._room_owners[new_channel.id] = member.id
            await self._grant_room_owner(new_channel, member)
            try:
                await member.move_to(new_channel)
            except discord.HTTPException:
                log.debug("Could not move member into new room", exc_info=True)

            await self._post_room_controls(new_channel, member)

            task = asyncio.create_task(
                self._cleanup_room(new_channel.id, guild.id, hub["id"])
            )
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)
        except Exception:
            log.exception("Failed to create an autoroom temp room")

    async def _cleanup_room(self, channel_id, guild_id, hub_id):
        """Delete the temp room once it empties, then forget it."""
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    self._active[(guild_id, hub_id)].discard(channel_id)
                    self._forget_room(channel_id)
                    return
                if len(channel.members) == 0:
                    try:
                        await channel.delete()
                    except discord.HTTPException:
                        pass
                    self._active[(guild_id, hub_id)].discard(channel_id)
                    self._forget_room(channel_id)
                    return
            except Exception:
                log.exception("Failed to clean up temp room %s", channel_id)
                return

    # ------------------------------------------------------------------
    # Startup re-adoption (survive a restart)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        """Re-adopt temp rooms left behind by a restart, exactly once.

        This is the guaranteed survive-restart mechanism: it needs no persistent
        views and no per-room DB state. ``on_ready`` can fire repeatedly on
        reconnects, so a flag keeps the sweep to a single run. It must never
        crash startup, hence the broad guard.
        """
        if self._adopted:
            return
        self._adopted = True
        try:
            await self._adopt_existing_rooms()
        except Exception:
            log.exception("Autoroom startup re-adoption failed")

    async def _adopt_existing_rooms(self):
        """Sweep every configured hub's category for orphaned temp rooms."""
        for guild_id, hub_map in list(self._hub_index.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            for hub in list(hub_map.values()):
                try:
                    await self._adopt_hub_rooms(guild, hub)
                except Exception:
                    log.exception(
                        "Failed to re-adopt rooms for hub %s in guild %s",
                        hub.get("id"),
                        guild_id,
                    )

    async def _adopt_hub_rooms(self, guild, hub):
        """Re-adopt (or reap) the temp rooms sitting in one hub's category.

        Every voice channel in the hub's category other than the trigger channel
        is treated as a temp room: empty ones are reaped, live ones are re-armed.
        """
        category_id = hub.get("category_id")
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            return
        hub_channel_id = hub.get("hub_channel_id")
        for channel in list(category.voice_channels):
            if channel.id == hub_channel_id:
                continue  # the join-to-create trigger, never a temp room
            try:
                await self._adopt_room(channel, guild, hub)
            except Exception:
                log.exception("Failed to re-adopt temp room %s", channel.id)

    async def _adopt_room(self, channel, guild, hub):
        """Reap an empty leftover room, or re-arm a live one after a restart.

        Live rooms: re-seed ownership from the channel's ``manage_channels``
        overwrite (may be absent -> left unowned so the panel offers Claim),
        purge the stale inert control panel, post a fresh one, and re-arm the
        empty-room reaper. All Discord calls are best-effort.
        """
        if len(channel.members) == 0:
            try:
                await channel.delete()
            except discord.HTTPException:
                log.debug("Could not reap empty leftover room", exc_info=True)
            self._forget_room(channel.id)
            return

        owner_id = self._owner_from_channel(channel)
        if owner_id is not None:
            self._room_owners[channel.id] = owner_id
        self._active[(guild.id, hub["id"])].add(channel.id)

        await self._purge_stale_panels(channel)
        await self._post_room_controls(channel, None)

        task = asyncio.create_task(
            self._cleanup_room(channel.id, guild.id, hub["id"])
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _purge_stale_panels(self, channel):
        """Delete the bot's own recent messages in a room's voice text chat.

        After a restart the old control panel is inert (its view is dead), so we
        clear the bot's recent messages before posting a fresh panel. Deleted one
        by one (not bulk) so it works regardless of message age and needs no
        manage_messages permission for the bot's own messages. Best-effort.
        """
        me = self.bot.user
        if me is None:
            return
        try:
            await channel.purge(
                limit=10,
                check=lambda message: message.author.id == me.id,
                bulk=False,
            )
        except (discord.HTTPException, AttributeError):
            log.debug("Could not purge stale room panels", exc_info=True)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(
        name="autoroom",
        aliases=["auto_room", "autorooms", "auto_rooms", "room", "rooms"],
        fallback="panel",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def autoroom(self, ctx):
        """Open the autoroom hub setup panel."""
        await self._send_panel(ctx)

    async def _send_panel(self, ctx):
        hubs = await self._load_hubs(ctx.guild.id)
        view = AutoroomPanel(
            self,
            ctx.author.id,
            ctx.guild.id,
            hubs,
            used_channels=len(ctx.guild.channels),
        )
        message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        view.message = message

    @autoroom.command(name="list", aliases=["ls"])
    @commands.guild_only()
    async def list_autorooms(self, ctx):
        """List the configured autoroom hubs in this server."""
        hubs = await self._load_hubs(ctx.guild.id)
        if not hubs:
            await ctx.send(_("There are no autoroom hubs set up in this server."))
            return

        embed = discord.Embed(
            title=_("Autoroom hubs"), colour=discord.Colour.blurple()
        )
        for hub in hubs:
            channel = ctx.guild.get_channel(hub.get("hub_channel_id"))
            location = channel.mention if channel else _("channel missing")
            embed.add_field(
                name=hub.get("label") or DEFAULT_LABEL,
                value=_("{location}\n{summary}").format(
                    location=location, summary=summarise_hub(hub)
                ),
                inline=False,
            )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(TemporaryRooms(bot))
