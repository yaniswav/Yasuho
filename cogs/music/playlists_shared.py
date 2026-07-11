"""Guild-shared music playlists: save the live queue as a named server playlist
anyone can load later (creator/moderators manage them).

This is a cog mixin folded into :class:`cogs.music.music.Music` (the same
composition the AniList package uses). It owns the ``/serverplaylist`` hybrid
group and its ``save``/``play``/``list``/``delete``/``rename`` subcommands, plus
the small pure helpers behind them (name normalisation, cap decisions, the save
snapshot shape, decode-failure accounting, the creator-or-moderator permission
decision). The commands lean on seams already provided by the Music cog -
``_require_player``, ``_snapshot``, ``_has_manage_guild``, ``_nodes_available``
and ``bot.sl_client.decode_tracks`` (the exact no-re-search decode seam the cold
restore uses in ``_restore_one``) - so this module adds a feature, not a second
copy of the engine.

Scale: the ``guild_playlists`` table is hard-bounded per guild -
:data:`MAX_GUILD_PLAYLISTS` playlists, each at most :data:`MAX_PLAYLIST_TRACKS`
encoded tracks - and every cap is enforced in code (an INSERT cap guard mirroring
``add_favourite``, plus a pre-save track-count refusal), so a guild's rows and
its stored-blob footprint cannot grow without bound. Autocomplete is one indexed
prefix scan (LIMIT 25) per keystroke over the ``(guild_id, name_norm)`` primary
key; list is one indexed query (LIMIT 25); play decodes the stored blobs in a
single bulk Lavalink round trip (bounded at the track cap). Save/play/list are
explicit user actions on no background loop, so they need no quota.
"""

from __future__ import annotations

import logging
import typing

import discord
from discord import app_commands
from discord.ext import commands

from tools.formats import random_colour
from tools.i18n import _, ngettext

log = logging.getLogger(__name__)

# Hard per-guild caps. 25 named playlists per guild; 200 encoded tracks each.
# Enforced in code (the INSERT cap guard and the pre-save track-count refusal),
# so the table and its stored-blob footprint stay bounded.
MAX_GUILD_PLAYLISTS = 25
MAX_PLAYLIST_TRACKS = 200

# Longest playlist name we accept (characters, after whitespace cleanup). Well
# under Discord's 100-char autocomplete-choice limit so a name always renders.
MAX_NAME_LEN = 60


# ---------------------------------------------------------------------------
# Pure helpers (no I/O - unit tested in isolation)
# ---------------------------------------------------------------------------


def clean_name(raw: typing.Optional[str]) -> str:
    """Collapse whitespace and trim a user-supplied playlist name (display form)."""
    return " ".join((raw or "").split())


def normalize_name(raw: typing.Optional[str]) -> str:
    """Case-insensitive uniqueness key for a name (whitespace-clean, casefolded).

    Two names that differ only in case or surrounding/internal whitespace map to
    the same key, so ``guild_playlists``'s ``(guild_id, name_norm)`` primary key
    enforces one playlist per name per guild regardless of how it was typed.
    """
    return clean_name(raw).casefold()


def name_error(display: str) -> typing.Optional[str]:
    """Return a reason code for an unusable cleaned name, else ``None``.

    Codes: ``"empty"`` (nothing left after cleanup), ``"too_long"`` (over
    :data:`MAX_NAME_LEN`). Pure - callers map the code to localised prose.
    """
    if not display:
        return "empty"
    if len(display) > MAX_NAME_LEN:
        return "too_long"
    return None


def track_cap_error(count: int) -> typing.Optional[str]:
    """Return a reason code when a save's track count is unusable, else ``None``.

    Codes: ``"empty"`` (nothing to save), ``"too_many"`` (over
    :data:`MAX_PLAYLIST_TRACKS`). Pure decision helper.
    """
    if count <= 0:
        return "empty"
    if count > MAX_PLAYLIST_TRACKS:
        return "too_many"
    return None


def guild_cap_reached(existing_count: int) -> bool:
    """True when a guild already holds the maximum number of playlists."""
    return existing_count >= MAX_GUILD_PLAYLISTS


def snapshot_tracks(player: typing.Any) -> typing.Tuple[typing.List[str], int]:
    """Assemble the encoded blobs and total duration to save from a live player.

    The current track first (so a load replays in the same order), then the
    user-lane queue (``player.queue.tracks`` - not the hidden autoplay lane). Any
    track lacking an ``encoded`` blob is skipped. Returns
    ``(encoded_blobs, total_ms)``. Pure over the player/track/queue shapes.
    """
    current = getattr(player, "current", None)
    queue = getattr(player, "queue", None)
    lane = list(getattr(queue, "tracks", None) or ())

    encoded: typing.List[str] = []
    total_ms = 0
    for track in (current, *lane):
        if track is None:
            continue
        blob = getattr(track, "encoded", None)
        if not blob:
            continue
        encoded.append(blob)
        total_ms += int(getattr(track, "length", 0) or 0)
    return encoded, total_ms


def account_decoded(
    decoded: typing.Optional[typing.Sequence[typing.Any]], stored_count: int
) -> typing.Tuple[typing.List[typing.Any], int]:
    """Split a decode result into ``(usable_tracks, skipped_count)``.

    ``decoded`` may contain ``None`` entries (Lavalink could not decode a stale
    blob after a major bump) and may be shorter than ``stored_count`` (a
    truncated / failed batch); both cases count as skips so a load never aborts
    on a bad track. Usable tracks are the non-``None`` entries. Pure - the play
    command turns the counts into a "queued N, skipped M" line.
    """
    usable = [track for track in (decoded or []) if track is not None]
    skipped = max(stored_count - len(usable), 0)
    return usable, skipped


def can_manage(actor: typing.Any, creator_id: int, has_manage_guild: bool) -> bool:
    """True when ``actor`` may delete/rename a playlist: its creator or a moderator.

    ``has_manage_guild`` is threaded in from ``Music._has_manage_guild(actor)`` so
    this stays pure and unit-testable without a real discord Member (mirrors the
    effects ``is_effect_exempt`` seam).
    """
    return bool(has_manage_guild) or getattr(actor, "id", None) == creator_id


def _like_prefix(term: str) -> str:
    """Escape LIKE wildcards in ``term`` and append ``%`` for a prefix match.

    ``%`` / ``_`` / ``\\`` in a user's typed prefix are escaped (the query uses
    ``ESCAPE '\\'``) so they match literally instead of acting as wildcards.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


# ---------------------------------------------------------------------------
# Read-only list card (Components V2, house style)
# ---------------------------------------------------------------------------


class _PlaylistListCard(discord.ui.LayoutView):
    """The guild's server playlists as one accent Container (read-only, public).

    A display-only Components V2 card in the music house style: a heading and one
    line per playlist (name, track count, duration, creator mention, saved date).
    No interactive items, so no author gate and no timeout handling is needed.
    """

    def __init__(self, guild_name: str, rows: typing.Sequence[typing.Mapping]) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                _("### 🎵 Server Playlists - {guild}").format(guild=guild_name)
            )
        )
        lines: typing.List[str] = []
        for row in rows:
            count = int(row["count"])
            head = ngettext(
                "**{name}** - {count} track - {duration}",
                "**{name}** - {count} tracks - {duration}",
                count,
            ).format(name=row["name"], count=count, duration=row["duration"])
            meta = _("-# by {creator} - saved {date}").format(
                creator=f"<@{row['creator_id']}>",
                date=f"<t:{row['created_ts']}:D>",
            )
            lines.append(head + "\n" + meta)
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)


# ---------------------------------------------------------------------------
# Cog mixin
# ---------------------------------------------------------------------------


class ServerPlaylistMixin:
    """Cog mixin: the ``/serverplaylist`` group (shared, per-guild playlists)."""

    # -- Database access (all bounded by the per-guild caps) ---------------

    async def _guild_playlist_count(self, guild_id: int) -> int:
        """How many playlists a guild currently holds (for the cap check)."""
        value = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM guild_playlists WHERE guild_id = $1", guild_id
        )
        return int(value or 0)

    async def _save_guild_playlist(
        self,
        guild_id: int,
        display: str,
        norm: str,
        creator_id: int,
        tracks: typing.Sequence[str],
        total_ms: int,
    ) -> str:
        """Insert a new playlist, guarding the per-guild cap in the statement.

        Returns ``"saved"`` on a new row, ``"exists"`` if the name is already
        taken (case-insensitively), or ``"full"`` when the guild is at the cap.
        The INSERT only fires while under the cap and skips on a name conflict, so
        growth stays bounded - mirrors ``Music.add_favourite``.
        """
        status = await self.bot.db_pool.execute(
            """
            INSERT INTO guild_playlists
                (guild_id, name, name_norm, creator_id, tracks, track_count, total_ms)
            SELECT $1, $2, $3, $4, $5, $6, $7
            WHERE (SELECT COUNT(*) FROM guild_playlists WHERE guild_id = $1) < $8
            ON CONFLICT (guild_id, name_norm) DO NOTHING
            """,
            guild_id,
            display,
            norm,
            creator_id,
            list(tracks),
            len(tracks),
            int(total_ms),
            MAX_GUILD_PLAYLISTS,
        )
        if status.rsplit(" ", 1)[-1] == "1":
            return "saved"
        exists = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM guild_playlists WHERE guild_id = $1 AND name_norm = $2",
            guild_id,
            norm,
        )
        return "exists" if exists else "full"

    async def _fetch_guild_playlist(
        self, guild_id: int, norm: str
    ) -> typing.Optional[typing.Mapping]:
        """Fetch one playlist row by its normalised name (or ``None``)."""
        return await self.bot.db_pool.fetchrow(
            """
            SELECT name, creator_id, tracks, track_count, total_ms, created_at
            FROM guild_playlists
            WHERE guild_id = $1 AND name_norm = $2
            """,
            guild_id,
            norm,
        )

    async def _list_guild_playlists(self, guild_id: int) -> typing.List[typing.Mapping]:
        """Every playlist in a guild, newest first (bounded by the cap)."""
        return await self.bot.db_pool.fetch(
            """
            SELECT name, creator_id, track_count, total_ms, created_at
            FROM guild_playlists
            WHERE guild_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            guild_id,
            MAX_GUILD_PLAYLISTS,
        )

    async def _autocomplete_playlists(
        self, guild_id: int, prefix_norm: str
    ) -> typing.List[typing.Mapping]:
        """Names matching a normalised prefix, cheap and bounded (LIMIT 25).

        One indexed prefix scan over the ``(guild_id, name_norm)`` primary key -
        the per-keystroke cost of the play autocomplete.
        """
        return await self.bot.db_pool.fetch(
            """
            SELECT name
            FROM guild_playlists
            WHERE guild_id = $1 AND name_norm LIKE $2 ESCAPE '\\'
            ORDER BY name_norm
            LIMIT $3
            """,
            guild_id,
            _like_prefix(prefix_norm),
            MAX_GUILD_PLAYLISTS,
        )

    async def _delete_guild_playlist(self, guild_id: int, norm: str) -> bool:
        """Delete a playlist by normalised name; True when a row was removed."""
        status = await self.bot.db_pool.execute(
            "DELETE FROM guild_playlists WHERE guild_id = $1 AND name_norm = $2",
            guild_id,
            norm,
        )
        return status.rsplit(" ", 1)[-1] != "0"

    async def _rename_guild_playlist(
        self, guild_id: int, old_norm: str, new_display: str, new_norm: str
    ) -> None:
        """Rename a playlist's display name and normalised key (caller pre-checks)."""
        await self.bot.db_pool.execute(
            """
            UPDATE guild_playlists
            SET name = $3, name_norm = $4
            WHERE guild_id = $1 AND name_norm = $2
            """,
            guild_id,
            old_norm,
            new_display,
            new_norm,
        )

    # -- Shared command helpers -------------------------------------------

    async def _connect_for_playlist(self, ctx):
        """Reuse the active player or join the caller's voice channel (or ``None``).

        Mirrors the connect seam in ``playlist_play`` / ``_play_query``: an already
        active player is reused; otherwise the caller must be in a voice channel,
        which the bot joins as a fresh session. Sends the reason and returns
        ``None`` on failure.
        """
        # Player and Music seams live in music.py; import lazily to avoid the
        # package import cycle (music.py imports this module at class definition).
        from cogs.music.music import Player

        player = ctx.voice_client
        if player is not None:
            if player.home is None:
                player.home = ctx.channel
            return player

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send(_("You must be in a voice channel first."))
            return None
        try:
            player = await ctx.author.voice.channel.connect(cls=Player)
        except discord.ClientException:
            log.exception("Failed to connect to the voice channel")
            await ctx.send(
                _("I was unable to join your voice channel. Please try again.")
            )
            return None
        player.dj = ctx.author
        player.home = ctx.channel
        await self._init_autoplay(player, ctx.author.id)
        from cogs.music import sponsorblock

        sponsorblock.schedule_apply(player)
        return player

    # -- The /serverplaylist group ----------------------------------------

    @commands.hybrid_group(
        name="serverplaylist",
        aliases=["splaylist", "spl"],
        fallback="list",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def serverplaylist(self, ctx: commands.Context) -> None:
        """Show this server's shared playlists (the bare-group fallback)."""
        await self._serverplaylist_list(ctx)

    @serverplaylist.command(name="save")
    @commands.guild_only()
    @app_commands.describe(name="A name for the new server playlist.")
    async def serverplaylist_save(
        self, ctx: commands.Context, *, name: str
    ) -> None:
        """Save the current track and queue as a shared server playlist."""
        display = clean_name(name)
        problem = name_error(display)
        if problem == "empty":
            await ctx.send(_("Give the playlist a name."))
            return
        if problem == "too_long":
            await ctx.send(
                _("Keep the playlist name under {max} characters.").format(
                    max=MAX_NAME_LEN
                )
            )
            return

        # Anyone in the bot's voice channel may save; _require_player enforces
        # both "connected" and "you are in my channel".
        player = await self._require_player(ctx, in_channel=True)
        if player is None:
            return
        if getattr(player, "current", None) is None:
            await ctx.send(
                _("Nothing is playing - start some music before saving a playlist.")
            )
            return

        tracks, total_ms = snapshot_tracks(player)
        cap = track_cap_error(len(tracks))
        if cap == "empty":
            await ctx.send(
                _("Nothing is playing - start some music before saving a playlist.")
            )
            return
        if cap == "too_many":
            await ctx.send(
                _(
                    "The queue is too long to save - a server playlist holds up "
                    "to {max} tracks. Trim it and try again."
                ).format(max=MAX_PLAYLIST_TRACKS)
            )
            return

        if guild_cap_reached(await self._guild_playlist_count(ctx.guild.id)):
            await ctx.send(
                _(
                    "This server already has the maximum of {max} playlists. "
                    "Delete one first."
                ).format(max=MAX_GUILD_PLAYLISTS)
            )
            return

        result = await self._save_guild_playlist(
            ctx.guild.id,
            display,
            normalize_name(display),
            ctx.author.id,
            tracks,
            total_ms,
        )
        if result == "exists":
            await ctx.send(
                _(
                    "A playlist called **{name}** already exists here. Pick "
                    "another name or delete it first."
                ).format(name=display)
            )
            return
        if result == "full":
            await ctx.send(
                _(
                    "This server already has the maximum of {max} playlists. "
                    "Delete one first."
                ).format(max=MAX_GUILD_PLAYLISTS)
            )
            return

        from cogs.music.music import format_clock

        count = len(tracks)
        await ctx.send(
            ngettext(
                "Saved **{name}** with {count} track ({duration}).",
                "Saved **{name}** with {count} tracks ({duration}).",
                count,
            ).format(name=display, count=count, duration=format_clock(total_ms))
        )

    @serverplaylist.command(name="play")
    @commands.guild_only()
    @app_commands.describe(name="Which server playlist to queue.")
    async def serverplaylist_play(
        self, ctx: commands.Context, *, name: str
    ) -> None:
        """Queue every track in a shared server playlist and start playing."""
        await ctx.defer()

        if not self._nodes_available():
            await ctx.send(
                _("Music is currently unavailable - no Lavalink node is connected.")
            )
            return

        row = await self._fetch_guild_playlist(ctx.guild.id, normalize_name(name))
        if row is None:
            await ctx.send(
                _("There's no server playlist called **{name}**.").format(
                    name=clean_name(name)
                )
            )
            return

        stored = list(row["tracks"] or [])
        try:
            decoded = await self.bot.sl_client.decode_tracks(*stored)
        except RuntimeError:
            log.exception("Server playlist decode failed: no node available")
            await ctx.send(
                _("Music is currently unavailable - no Lavalink node is connected.")
            )
            return
        usable, skipped = account_decoded(decoded, len(stored))

        if not usable:
            await ctx.send(
                _("None of **{name}**'s tracks could be loaded right now.").format(
                    name=row["name"]
                )
            )
            return

        player = await self._connect_for_playlist(ctx)
        if player is None:
            return

        for track in usable:
            track.extras.requester = ctx.author.id
            player.queue.put(track)

        # Loading a playlist is an explicit choice: it ends any radio session.
        player.radio_genre = None
        if not player.current:
            await player.play(player.queue.get())
        await self._snapshot(player)

        count = len(usable)
        message = ngettext(
            "Queued {count} track from **{name}**.",
            "Queued {count} tracks from **{name}**.",
            count,
        ).format(count=count, name=row["name"])
        if skipped:
            message += ngettext(
                " {skipped} track was skipped - it could not be loaded.",
                " {skipped} tracks were skipped - they could not be loaded.",
                skipped,
            ).format(skipped=skipped)
        await ctx.send(message)

    @serverplaylist_play.autocomplete("name")
    async def _serverplaylist_play_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice[str]]:
        """Suggest the guild's playlist names for a normalised prefix (bounded)."""
        if interaction.guild is None:
            return []
        try:
            rows = await self._autocomplete_playlists(
                interaction.guild.id, normalize_name(current)
            )
        except Exception:
            log.exception("Server playlist autocomplete failed")
            return []
        return [
            app_commands.Choice(name=row["name"][:100], value=row["name"])
            for row in rows
        ]

    async def _serverplaylist_list(self, ctx: commands.Context) -> None:
        """Send the read-only Components V2 card of the guild's playlists."""
        from cogs.music.music import format_clock

        rows = await self._list_guild_playlists(ctx.guild.id)
        if not rows:
            await ctx.send(
                _(
                    "This server has no saved playlists yet. Use "
                    "`/serverplaylist save` to create one."
                )
            )
            return

        prepared = [
            {
                "name": row["name"],
                "count": row["track_count"],
                "duration": format_clock(int(row["total_ms"] or 0)),
                "creator_id": row["creator_id"],
                "created_ts": int(row["created_at"].timestamp()),
            }
            for row in rows
        ]
        await ctx.send(
            view=_PlaylistListCard(ctx.guild.name, prepared),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @serverplaylist.command(name="delete", aliases=["remove", "rm", "del"])
    @commands.guild_only()
    @app_commands.describe(name="Which server playlist to delete.")
    async def serverplaylist_delete(
        self, ctx: commands.Context, *, name: str
    ) -> None:
        """Delete a shared server playlist (its creator or a moderator only)."""
        row = await self._fetch_guild_playlist(ctx.guild.id, normalize_name(name))
        if row is None:
            await ctx.send(
                _("There's no server playlist called **{name}**.").format(
                    name=clean_name(name)
                )
            )
            return
        if not can_manage(
            ctx.author, row["creator_id"], self._has_manage_guild(ctx.author)
        ):
            await ctx.send(
                _("Only the playlist's creator or a moderator can do that.")
            )
            return
        await self._delete_guild_playlist(ctx.guild.id, normalize_name(name))
        await ctx.send(
            _("Deleted the server playlist **{name}**.").format(name=row["name"])
        )

    @serverplaylist_delete.autocomplete("name")
    async def _serverplaylist_delete_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice[str]]:
        """Reuse the play autocomplete for the delete name argument."""
        return await self._serverplaylist_play_autocomplete(interaction, current)

    @serverplaylist.command(name="rename")
    @commands.guild_only()
    @app_commands.describe(
        old="The playlist to rename.", new="The new name for it."
    )
    async def serverplaylist_rename(
        self, ctx: commands.Context, old: str, *, new: str
    ) -> None:
        """Rename a shared server playlist (its creator or a moderator only)."""
        row = await self._fetch_guild_playlist(ctx.guild.id, normalize_name(old))
        if row is None:
            await ctx.send(
                _("There's no server playlist called **{name}**.").format(
                    name=clean_name(old)
                )
            )
            return
        if not can_manage(
            ctx.author, row["creator_id"], self._has_manage_guild(ctx.author)
        ):
            await ctx.send(
                _("Only the playlist's creator or a moderator can do that.")
            )
            return

        new_display = clean_name(new)
        problem = name_error(new_display)
        if problem == "empty":
            await ctx.send(_("Give the playlist a name."))
            return
        if problem == "too_long":
            await ctx.send(
                _("Keep the playlist name under {max} characters.").format(
                    max=MAX_NAME_LEN
                )
            )
            return

        old_norm = normalize_name(old)
        new_norm = normalize_name(new_display)
        # A different playlist already owns the new name? Refuse. (A case-only
        # rename keeps the same norm and is allowed - it just updates display.)
        if new_norm != old_norm:
            clash = await self._fetch_guild_playlist(ctx.guild.id, new_norm)
            if clash is not None:
                await ctx.send(
                    _(
                        "A playlist called **{name}** already exists here. Pick "
                        "another name or delete it first."
                    ).format(name=new_display)
                )
                return

        await self._rename_guild_playlist(
            ctx.guild.id, old_norm, new_display, new_norm
        )
        await ctx.send(
            _("Renamed **{old}** to **{new}**.").format(
                old=row["name"], new=new_display
            )
        )

    @serverplaylist_rename.autocomplete("old")
    async def _serverplaylist_rename_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice[str]]:
        """Reuse the play autocomplete for the rename ``old`` argument."""
        return await self._serverplaylist_play_autocomplete(interaction, current)
