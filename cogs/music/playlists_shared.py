"""Guild-shared music playlists: save the live queue as a named server playlist
anyone can load later (creator/moderators manage them).

This is a cog mixin folded into :class:`cogs.music.music.Music` (the same
composition the AniList package uses). It owns the ``/serverplaylist`` hybrid
group and its ``save``/``play``/``list``/``delete``/``rename`` subcommands, plus
the small pure helpers behind them (name normalisation, cap decisions, the save
snapshot shape, decode-failure accounting, the creator-or-moderator permission
decision). The commands lean on seams already provided by the Music cog -
``_require_player``, ``_snapshot``, ``_has_manage_guild``, ``_nodes_available``,
``_init_session`` (the shared player-birth configuration seam: autoplay seed,
this guild's default volume, SponsorBlock) and ``bot.sl_client.decode_tracks``
(the exact no-re-search decode seam the cold restore uses in ``_restore_one``) -
so this module adds a feature, not a second copy of the engine.

Scale: the ``guild_playlists`` table is hard-bounded per guild -
:data:`MAX_GUILD_PLAYLISTS` playlists, each at most :data:`MAX_PLAYLIST_TRACKS`
encoded tracks (the FREE values - see "PREMIUM CAPS" below) - and every cap is
enforced in code (an INSERT cap guard mirroring ``add_favourite``, plus a
pre-save track-count refusal), so a guild's rows and its stored-blob footprint
cannot grow without bound. Autocomplete is one indexed prefix scan (LIMIT 25,
Discord's own hard choices cap - :data:`AUTOCOMPLETE_LIMIT`) per keystroke over
the ``(guild_id, name_norm)`` primary key; the list card and the archival
classification each read every row up to the ABSOLUTE safety ceiling (so an
archived excess is never hidden by a query bound sized for the ordinary case);
play decodes the stored blobs in a single bulk Lavalink round trip (bounded at
the per-playlist track cap). Save/play/list are explicit user actions on no
background loop, so they need no quota.

PREMIUM CAPS (M4a-1, .claude/plans/monetisation/4-plan-retenu.md).
:data:`MAX_GUILD_PLAYLISTS`/:data:`MAX_PLAYLIST_TRACKS` stay exactly what they
were - the FREE values :mod:`tools.premium` restates and
tests/tools/test_premium.py's drift guard checks against. Every COMMAND below
instead reads the EFFECTIVE caps from ``bot.premium.for_guild(guild_id)``
(:class:`tools.premium.GuildLimits`'s ``max_guild_playlists``/
``max_playlist_tracks``), so a Yasuho+ guild saves more and bigger playlists
without this module's own constants ever drifting from the free catalog entry.
When that effective cap drops below what a guild already has on record - a
subscription lapsed, was refunded, or was revoked - nothing is deleted or
truncated: the excess is ARCHIVED (:mod:`tools.premium_archive`'s lazy,
computed-at-use-time rule). An archived playlist stays listed (marked as
such), can still be deleted, but cannot be loaded (``/serverplaylist play``)
or edited (``/serverplaylist rename``) until the cap rises again or it is
deleted. Two independent ways a playlist ends up archived:

* the guild holds MORE playlists than ``max_guild_playlists`` allows - the
  oldest excess (by ``created_at``, ties broken by ``name_norm`` - see
  :func:`tools.premium_archive.classify`) is archived, exactly as many as it
  takes to bring the guild back under its current cap;
* the playlist's OWN ``track_count`` exceeds the current
  ``max_playlist_tracks`` - it was saved at a higher tier and the guild has
  since dropped to one with a smaller per-playlist cap. This one is checked
  per-row, independent of the guild's count-cap classification.
"""

from __future__ import annotations

import datetime
import logging
import random
import typing

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import always_on, playlist_sync, safetext
from cogs.music.player import VoiceConnectFailed, connect_player
from tools import premium, premium_upsell
from tools.formats import random_colour
from tools.i18n import _, ngettext
from tools.premium_archive import ArchivalResult
from tools.premium_archive import classify as classify_archival
from tools.views import AuthorView, LocaleLayoutView

log = logging.getLogger(__name__)

# FREE per-guild caps (M4a-1: see the module docstring's "PREMIUM CAPS" section
# for how a Yasuho+ guild gets a higher EFFECTIVE cap without these changing).
# 25 named playlists per guild; 200 encoded tracks each. Enforced in code (the
# INSERT cap guard and the pre-save track-count refusal, both now parameterised
# on the caller's effective cap rather than these constants directly), so the
# table and its stored-blob footprint stay bounded.
MAX_GUILD_PLAYLISTS = 25
MAX_PLAYLIST_TRACKS = 200

# Discord's own hard cap on autocomplete choices - fixed by the platform, not
# by our commercial tiers. Deliberately its OWN constant rather than reusing
# MAX_GUILD_PLAYLISTS (today both happen to be 25): a Yasuho+ guild's effective
# cap is 75, and autocomplete must still never ask Discord for more than 25
# choices regardless of how many playlists (active or archived) a guild holds.
AUTOCOMPLETE_LIMIT = 25

# Longest playlist name we accept (characters, after whitespace cleanup). Well
# under Discord's 100-char autocomplete-choice limit so a name always renders.
MAX_NAME_LEN = 60

# The house pattern for a message carrying third-party text. The bot's global
# allowed_mentions leaves USER mentions enabled (core.py), so a public reply that
# echoes a name a member typed would happily ping whoever they named - repeatedly,
# from a command with no cooldown. Every send in this module that interpolates a
# playlist name passes this, paired with :func:`echo_name` for the markdown.
NO_PINGS = discord.AllowedMentions.none()


# ---------------------------------------------------------------------------
# Pure helpers (no I/O - unit tested in isolation)
# ---------------------------------------------------------------------------


def clean_name(raw: typing.Optional[str]) -> str:
    """Collapse whitespace and trim a user-supplied playlist name (display form)."""
    return " ".join((raw or "").split())


def echo_name(raw: typing.Optional[str]) -> str:
    """A playlist name made safe to quote back into a PUBLIC message.

    Every ``/serverplaylist`` reply quotes a name as ``**{name}**``, and two
    different kinds of name reach those messages: one the caller just typed
    (the not-found and clash replies, which echo an argument that was never
    stored and, on the delete / play / rename lookups, never even length-checked)
    and one read back from the row (which some other member typed earlier). Both
    are third-party text in a message the bot signs, so both come through here.

    :func:`safetext.public_echo` clips it - a 2000-character argument could
    otherwise push the reply past Discord's message limit and make the command
    fail outright - and neutralises the markdown, so a playlist named
    ``[click here](https://evil.example)`` is quoted as visible text rather than
    published as a link the bot appears to vouch for.

    It is only half the guard. The other half is at the send:
    ``allowed_mentions=discord.AllowedMentions.none()``, which is what stops a
    name of ``<@1234>`` from pinging (the bot's global allowed_mentions permits
    user mentions, so this cannot be left to the default). Neither half is
    optional and they always travel together - see :data:`NO_PINGS`.
    """
    return safetext.public_echo(clean_name(raw), limit=MAX_NAME_LEN + 20)


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


def track_cap_error(
    count: int, max_tracks: int = MAX_PLAYLIST_TRACKS
) -> typing.Optional[str]:
    """Return a reason code when a save's track count is unusable, else ``None``.

    Codes: ``"empty"`` (nothing to save), ``"too_many"`` (over ``max_tracks``).
    Pure decision helper. ``max_tracks`` defaults to the FREE value
    (:data:`MAX_PLAYLIST_TRACKS`) so existing callers/tests keep today's
    behaviour unchanged; a command passes the caller's EFFECTIVE cap
    (``bot.premium.for_guild(guild_id).max_playlist_tracks``) explicitly.
    """
    if count <= 0:
        return "empty"
    if count > max_tracks:
        return "too_many"
    return None


def guild_cap_reached(
    existing_count: int, max_guild: int = MAX_GUILD_PLAYLISTS
) -> bool:
    """True when a guild already holds ``max_guild`` playlists (or more).

    ``max_guild`` defaults to the FREE value (:data:`MAX_GUILD_PLAYLISTS`) for
    the same reason :func:`track_cap_error`'s default does; a command passes
    the caller's effective ``max_guild_playlists`` explicitly.
    """
    return existing_count >= max_guild


def playlist_is_archived(
    track_count: int, max_playlist_tracks: int, active_by_count: bool
) -> bool:
    """True when a playlist is ARCHIVED under the guild's CURRENT premium tier.

    Two independent reasons (see the module docstring's "PREMIUM CAPS"
    section): ``active_by_count`` is False (this playlist lost its slot under
    the guild's count cap - the caller already ran
    :func:`tools.premium_archive.classify` over every playlist in the guild
    and is handing in ITS verdict for this one row), or ``track_count``
    alone exceeds ``max_playlist_tracks`` (saved at a higher tier, the guild
    has since dropped to a smaller per-playlist cap). Either is enough on its
    own; this function never needs to know WHICH one applies - the caller's
    refusal message is the same either way ("archived").
    """
    return (not active_by_count) or track_count > max_playlist_tracks


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


# Discord caps the TEXT of a Components V2 message at 4000 characters in total
# (all TextDisplays together); the heading, the "more" footer and a long guild
# name need room, so the playlist lines get 3400.
LIST_TEXT_BUDGET = 3400


def fit_blocks(
    blocks: typing.Sequence[str], budget: int
) -> typing.Tuple[typing.List[str], int]:
    """``(shown, hidden_count)``: the leading blocks that fit ``budget`` chars.

    Blocks are joined by one newline; the first block always shows (a single
    row is far below any budget). Reserves 80 characters for the footer line.
    """
    shown: typing.List[str] = []
    used = 0
    for index, block in enumerate(blocks):
        cost = len(block) + 1
        if shown and used + cost > budget - 80:
            return shown, len(blocks) - index
        shown.append(block)
        used += cost
    return shown, 0


def link_line(row: typing.Mapping) -> typing.Optional[str]:
    """The "linked to <domain>" info line of a playlist row (``None`` if unlinked)."""
    url = row.get("source_url")
    if not url:
        return None
    domain = playlist_sync.source_domain(url)
    if playlist_sync.is_paused(url, row.get("next_sync_at")):
        return _("-# linked to {domain} - sync paused").format(domain=domain)
    synced = row.get("synced_at")
    if synced is None:
        return _("-# linked to {domain}").format(domain=domain)
    return _("-# linked to {domain} - last synced {when}").format(
        domain=domain, when=f"<t:{int(synced.timestamp())}:R>"
    )


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


class _PlaylistListCard(LocaleLayoutView):
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
                _("### 🎵 Server Playlists - {guild}").format(
                    guild=safetext.public_echo(guild_name)
                )
            )
        )
        blocks: typing.List[str] = []
        for row in rows:
            count = int(row["count"])
            head = ngettext(
                "**{name}** - {count} track - {duration}",
                "**{name}** - {count} tracks - {duration}",
                count,
            ).format(
                name=echo_name(row["name"]),
                count=count,
                duration=row["duration"],
            )
            if row.get("archived"):
                # Marked, not hushed - stays listed (deletable, exportable if an
                # export ever exists) but not loadable/editable. No /premium
                # pointer here: that one-line nudge belongs on the REFUSAL a
                # member hits trying to load/rename it, not on every passive
                # listing (the plan's own "jamais de ... pub hors contexte").
                head += " " + _("(archived)")
            meta = _("-# by {creator} - saved {date}").format(
                creator=f"<@{row['creator_id']}>",
                date=f"<t:{row['created_ts']}:D>",
            )
            link = row.get("link")
            if link:
                meta += "\n" + link
            blocks.append(head + "\n" + meta)
        lines, hidden = fit_blocks(blocks, LIST_TEXT_BUDGET)
        if hidden:
            lines.append(
                ngettext(
                    "... and {count} more playlist",
                    "... and {count} more playlists",
                    hidden,
                ).format(count=hidden)
            )
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)


class _ReplaceImportView(AuthorView):
    """Replace/Cancel prompt before an import overwrites an existing playlist.

    Author-gated. The tracks were already loaded (and clipped) when the prompt
    was built, so the click only writes; the buttons are built in ``__init__``
    so their labels follow the invoker's locale.
    """

    def __init__(self, cog, author, guild_id, norm, plan, *, timeout=60):
        super().__init__(
            author.id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.author = author
        self.cog = cog
        self.guild_id = guild_id
        self.norm = norm
        self.plan = plan
        confirm = discord.ui.Button(
            label=_("Replace"), style=discord.ButtonStyle.danger
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label=_("Cancel"), style=discord.ButtonStyle.secondary
        )
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction):
        try:
            text = await self._replace()
            await interaction.response.edit_message(
                content=text, embed=None, view=None, allowed_mentions=NO_PINGS
            )
        except Exception:
            log.exception("Playlist import replace failed")
            try:
                await interaction.response.edit_message(
                    content=_("Something went wrong."),
                    embed=None,
                    view=None,
                    allowed_mentions=NO_PINGS,
                )
            except discord.HTTPException:
                pass
        finally:
            self.stop()

    async def _replace(self) -> str:
        """Re-check the row against what the prompt showed, then write."""
        cog, plan = self.cog, self.plan
        row = await cog._fetch_guild_playlist(self.guild_id, self.norm)
        if row is None:
            return _("That playlist no longer exists.")
        changed = _("That playlist changed, nothing was replaced.")
        if (
            row["creator_id"] != plan["expect_creator"]
            or row["created_at"] != plan["expect_created"]
        ):
            return changed
        if not can_manage(
            self.author, row["creator_id"], cog._has_manage_guild(self.author)
        ):
            return _("Only the playlist's creator or a moderator can do that.")
        limits = cog.bot.premium.for_guild(self.guild_id)
        archival = await cog._guild_playlist_archival(
            self.guild_id, limits.max_guild_playlists
        )
        if playlist_is_archived(
            row["track_count"], limits.max_playlist_tracks, archival.is_active(self.norm)
        ):
            return changed
        result = await cog._replace_playlist(self.guild_id, self.norm, **plan)
        return {
            "replaced": _("Replaced the tracks of the playlist."),
            "link_full": _("This server already has the maximum of linked playlists."),
            "changed": changed,
        }.get(result, _("That playlist no longer exists."))

    async def _cancel(self, interaction):
        await interaction.response.edit_message(
            content=_("Cancelled - the playlist was not changed."),
            embed=None,
            view=None,
            allowed_mentions=NO_PINGS,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Cog mixin
# ---------------------------------------------------------------------------


class ServerPlaylistMixin:
    """Cog mixin: the ``/serverplaylist`` group (shared, per-guild playlists)."""

    async def _refuse_with_upsell(
        self, ctx, text, *, limit_key, benefit, kind="raise", allowed_mentions=None
    ):
        """Send a GUILD-scoped refusal's ``text``, with the upsell riding along.

        ``serverplaylist save`` never passes ``ephemeral=True`` to ``ctx.send``
        (the comment above the archived-playlist refusal in this same file
        explains why: ``ctx.defer()`` already committed the interaction to a
        PUBLIC response before any cap is even checked) - so on slash the base
        refusal stays exactly as it is, and the upsell goes out as a SEPARATE
        ephemeral followup; on prefix (no interaction, no followup, no
        component support) it is appended to the one and only message instead.

        ``allowed_mentions`` rides straight through to every ``ctx.send(text,
        ...)`` call (never onto the upsell's own followup, whose ``.line`` is
        always a translated static sentence, never third-party text): a
        caller whose ``text`` quotes a user-supplied name (a playlist's) must
        still pass ``NO_PINGS`` here exactly as it would on a plain
        ``ctx.send``, or the ping-injection guard this module's ``echo_name``
        exists for would quietly stop applying on this one path.
        """
        is_slash = premium_upsell.is_slash_context(ctx)
        upsell = await premium_upsell.for_guild_refusal(
            self.bot,
            limit_key=limit_key,
            guild_id=ctx.guild.id,
            person_id=ctx.author.id,
            is_admin=premium_upsell.invoker_is_admin(ctx.author),
            already_top_tier=premium_upsell.is_guild_already_top_tier(
                self.bot, ctx.guild.id
            ),
            benefit=benefit,
            kind=kind,
            allow_button=is_slash,
        )
        send_kwargs = {}
        if allowed_mentions is not None:
            send_kwargs["allowed_mentions"] = allowed_mentions
        if upsell is None:
            await ctx.send(text, **send_kwargs)
            return
        if not is_slash:
            await ctx.send(text + "\n" + upsell.line, **send_kwargs)
            return
        await ctx.send(text, **send_kwargs)
        followup_kwargs = {"ephemeral": True}
        view = upsell.view()
        if view is not None:
            followup_kwargs["view"] = view
        await ctx.interaction.followup.send(upsell.line, **followup_kwargs)

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
        max_guild_playlists: int = MAX_GUILD_PLAYLISTS,
    ) -> str:
        """Insert a new playlist, guarding the per-guild cap in the statement.

        Returns ``"saved"`` on a new row, ``"exists"`` if the name is already
        taken (case-insensitively), or ``"full"`` when the guild is at the cap.
        The INSERT only fires while under the cap and skips on a name conflict, so
        growth stays bounded - mirrors ``Music.add_favourite``. ``max_guild_playlists``
        defaults to the FREE value for callers (and tests) that do not pass one; the
        command itself always passes the caller's EFFECTIVE cap
        (``bot.premium.for_guild(guild_id).max_guild_playlists``).
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
            int(max_guild_playlists),
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
            SELECT name, creator_id, tracks, track_count, total_ms, created_at,
                   source_url, synced_at, next_sync_at
            FROM guild_playlists
            WHERE guild_id = $1 AND name_norm = $2
            """,
            guild_id,
            norm,
        )

    async def _fetch_guild_playlist_name(
        self, guild_id: int, norm: str
    ) -> typing.Optional[str]:
        """Just a playlist's display name (or ``None``) - never its blobs."""
        return await self.bot.db_pool.fetchval(
            "SELECT name FROM guild_playlists WHERE guild_id = $1 AND name_norm = $2",
            guild_id,
            norm,
        )

    async def _list_guild_playlists(self, guild_id: int) -> typing.List[typing.Mapping]:
        """Every playlist in a guild, newest first.

        Bounded by the ABSOLUTE safety ceiling
        (``premium.GUILD_CEILINGS["max_guild_playlists"]``), not by the
        guild's current effective cap: an archived excess (M4a-1) must stay
        visible on the list even when it is over today's cap, so this query
        can never stop short of returning it. ``name_norm`` rides along so
        the caller can classify every row's archived status without a second
        query.
        """
        return await self.bot.db_pool.fetch(
            """
            SELECT name, name_norm, creator_id, track_count, total_ms, created_at,
                   source_url, synced_at, next_sync_at
            FROM guild_playlists
            WHERE guild_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            guild_id,
            premium.GUILD_CEILINGS["max_guild_playlists"],
        )

    async def _guild_playlist_archival(
        self, guild_id: int, max_guild_playlists: int
    ) -> ArchivalResult:
        """Classify every playlist this guild holds against its count cap.

        Fetches ``(name_norm, created_at)`` for every row (bounded by the
        ABSOLUTE safety ceiling, same reasoning as :meth:`_list_guild_playlists`)
        and hands them to :func:`tools.premium_archive.classify` keyed by
        ``name_norm`` - the natural key every play/delete/rename lookup
        already uses, so a caller holding a normalised name can check
        ``result.is_active(norm)`` with no further lookup. No ``kept`` flag is
        set on any row today (that admin choice is a FUTURE lot per the
        plan); every row classifies purely by creation order.
        """
        rows = await self.bot.db_pool.fetch(
            """
            SELECT name_norm, created_at
            FROM guild_playlists
            WHERE guild_id = $1
            LIMIT $2
            """,
            guild_id,
            premium.GUILD_CEILINGS["max_guild_playlists"],
        )
        resources = [
            {"id": row["name_norm"], "created_at": row["created_at"]} for row in rows
        ]
        return classify_archival(resources, max_guild_playlists)

    async def _autocomplete_playlists(
        self, guild_id: int, prefix_norm: str
    ) -> typing.List[typing.Mapping]:
        """Names matching a normalised prefix, cheap and bounded (LIMIT 25).

        One indexed prefix scan over the ``(guild_id, name_norm)`` primary key -
        the per-keystroke cost of the play autocomplete. The bound is
        :data:`AUTOCOMPLETE_LIMIT` - Discord's own hard cap on autocomplete
        choices, independent of the guild's commercial playlist cap (which
        can be well over 25 for a Yasuho+ guild).
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
            AUTOCOMPLETE_LIMIT,
        )

    async def _delete_guild_playlist(self, guild_id: int, norm: str) -> bool:
        """Delete a playlist by normalised name; True when a row was removed.

        A 24/7 playlist pointing at it is cleared in the same transaction, so
        the radio never keeps a key no playlist answers to.
        """
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                status = await conn.execute(
                    "DELETE FROM guild_playlists WHERE guild_id = $1 AND name_norm = $2",
                    guild_id,
                    norm,
                )
                await always_on.retarget_radio_row(conn, guild_id, norm, None)
        self.always_on.retarget_radio(guild_id, norm, None)
        return status.rsplit(" ", 1)[-1] != "0"

    async def _rename_guild_playlist(
        self, guild_id: int, old_norm: str, new_display: str, new_norm: str
    ) -> None:
        """Rename a playlist's display name and normalised key (caller pre-checks).

        A 24/7 playlist pointing at it follows the new key in the same
        transaction (its play position is kept: same tracks, new name).
        """
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
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
                if new_norm != old_norm:
                    await always_on.retarget_radio_row(
                        conn, guild_id, old_norm, new_norm
                    )
        if new_norm != old_norm:
            self.always_on.retarget_radio(guild_id, old_norm, new_norm)

    async def _linked_count(self, guild_id: int) -> int:
        """How many of a guild's playlists are linked to a source."""
        value = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM guild_playlists "
            "WHERE guild_id = $1 AND source_url IS NOT NULL",
            guild_id,
        )
        return int(value or 0)

    async def _import_new_playlist(
        self,
        guild_id: int,
        display: str,
        norm: str,
        creator_id: int,
        tracks: typing.Sequence[str],
        total_ms: int,
        max_guild_playlists: int,
        *,
        source_url: typing.Optional[str] = None,
        next_sync_at: typing.Optional[datetime.datetime] = None,
        max_linked: int = 0,
    ) -> str:
        """Insert an imported playlist; the same cap guards as ``_save_guild_playlist``
        plus the linked-playlist cap, all inside the statement.

        Returns ``"saved"``, ``"exists"``, ``"link_full"`` (the LINKED cap is what
        blocked it) or ``"full"``.
        """
        status = await self.bot.db_pool.execute(
            """
            INSERT INTO guild_playlists
                (guild_id, name, name_norm, creator_id, tracks, track_count,
                 total_ms, source_url, synced_at, next_sync_at)
            SELECT $1, $2, $3, $4, $5, $6, $7, $8::text,
                   CASE WHEN $8::text IS NULL THEN NULL ELSE now() END, $9
            WHERE (SELECT COUNT(*) FROM guild_playlists WHERE guild_id = $1) < $10
              AND ($8::text IS NULL OR
                   (SELECT COUNT(*) FROM guild_playlists
                    WHERE guild_id = $1 AND source_url IS NOT NULL) < $11)
            ON CONFLICT (guild_id, name_norm) DO NOTHING
            """,
            guild_id,
            display,
            norm,
            creator_id,
            list(tracks),
            len(tracks),
            int(total_ms),
            source_url,
            next_sync_at,
            int(max_guild_playlists),
            int(max_linked),
        )
        if status.rsplit(" ", 1)[-1] == "1":
            return "saved"
        exists = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM guild_playlists WHERE guild_id = $1 AND name_norm = $2",
            guild_id,
            norm,
        )
        if exists:
            return "exists"
        if source_url and await self._linked_count(guild_id) >= int(max_linked):
            return "link_full"
        return "full"

    async def _replace_playlist(
        self,
        guild_id: int,
        norm: str,
        *,
        tracks: typing.Sequence[str],
        total_ms: int,
        source_url: typing.Optional[str] = None,
        next_sync_at: typing.Optional[datetime.datetime] = None,
        max_linked: int = 0,
        expect_creator: typing.Optional[int] = None,
        expect_created: typing.Optional[datetime.datetime] = None,
    ) -> str:
        """Overwrite an existing playlist's tracks (and its link) in place.

        Name, creator and created_at are kept (archival order must not move).
        ``source_url=None`` UNLINKS. ``expect_creator``/``expect_created`` pin
        the row the prompt was about: if it was deleted and recreated meanwhile
        the write matches nothing. Returns ``"replaced"``, ``"link_full"``,
        ``"changed"`` or ``"gone"``.
        """
        status = await self.bot.db_pool.execute(
            """
            UPDATE guild_playlists
            SET tracks = $3, track_count = $4, total_ms = $5,
                source_url = $6::text,
                synced_at = CASE WHEN $6::text IS NULL THEN NULL ELSE now() END,
                next_sync_at = $7, sync_failures = 0, sync_error = NULL
            WHERE guild_id = $1 AND name_norm = $2
              AND ($9::bigint IS NULL OR creator_id = $9)
              AND ($10::timestamptz IS NULL OR created_at = $10)
              AND ($6::text IS NULL OR source_url IS NOT NULL OR
                   (SELECT COUNT(*) FROM guild_playlists
                    WHERE guild_id = $1 AND source_url IS NOT NULL) < $8)
            """,
            guild_id,
            norm,
            list(tracks),
            len(tracks),
            int(total_ms),
            source_url,
            next_sync_at,
            int(max_linked),
            expect_creator,
            expect_created,
        )
        if status.rsplit(" ", 1)[-1] == "1":
            return "replaced"
        row = await self.bot.db_pool.fetchrow(
            "SELECT creator_id, created_at FROM guild_playlists "
            "WHERE guild_id = $1 AND name_norm = $2",
            guild_id,
            norm,
        )
        if row is None:
            return "gone"
        if (expect_creator is not None and row["creator_id"] != expect_creator) or (
            expect_created is not None and row["created_at"] != expect_created
        ):
            return "changed"
        return "link_full"

    async def _unlink_playlist(self, guild_id: int, norm: str) -> bool:
        """Drop a playlist's link (tracks stay); True when it was linked."""
        status = await self.bot.db_pool.execute(
            """
            UPDATE guild_playlists
            SET source_url = NULL, synced_at = NULL, next_sync_at = NULL,
                sync_failures = 0, sync_error = NULL
            WHERE guild_id = $1 AND name_norm = $2 AND source_url IS NOT NULL
            """,
            guild_id,
            norm,
        )
        return status.rsplit(" ", 1)[-1] == "1"

    # -- Shared command helpers -------------------------------------------

    async def _connect_for_playlist(self, ctx):
        """Reuse the active player or join the caller's voice channel (or ``None``).

        Mirrors the connect seam in ``playlist_play`` / ``_play_query``: an already
        active player is reused; otherwise the caller must be in a voice channel,
        which the bot joins as a fresh session configured by the shared
        ``Music._init_session`` player-birth seam. Sends the reason and returns
        ``None`` on failure.

        Reusing an EXISTING session takes the same-voice gate, for the same reason
        ``_play_query`` does: adding to a live queue is an act on the room that is
        listening. This path is the one that hurts most if it is left open - a
        server playlist is up to the guild's effective track cap at a time
        (the FREE value is 200; a Yasuho+ guild's is higher - see
        ``tools.premium.GuildLimits.max_playlist_tracks``) - and it is reached from
        ``/serverplaylist play`` and the favourites card alike. Starting a fresh
        session stays open (the connect below already requires the caller to be
        in a voice channel).
        """
        # The same-voice gate lives in music.py; import lazily to avoid the
        # package import cycle (music.py imports this module at class
        # definition). The connect seam itself comes from player.py, the
        # package's lowest layer, which has no such cycle - see the module
        # import above.
        from cogs.music.music import is_in_player_voice

        player = ctx.voice_client
        if player is not None:
            if not is_in_player_voice(player, ctx.author):
                await ctx.send(_("You must be in my voice channel to do that."))
                return None
            if player.home is None:
                player.home = ctx.channel
            return player

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send(_("You must be in a voice channel first."))
            return None
        try:
            player = await connect_player(ctx.author.voice.channel)
        except VoiceConnectFailed as exc:
            await ctx.send(exc.message)
            return None
        player.dj = ctx.author
        player.home = ctx.channel
        # Player birth: autoplay seed, this guild's default volume, and the
        # SponsorBlock categories - the same ``Music._init_session`` seam every
        # other fresh-connect entry point uses, so a per-guild music setting can
        # never reach three connect sites and be forgotten in this one.
        await self._init_session(player, ctx.author)
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

        # Effective caps (M4a-1): the free values unless this guild has Yasuho+ -
        # see tools.premium.GuildLimits.max_guild_playlists/max_playlist_tracks.
        limits = self.bot.premium.for_guild(ctx.guild.id)

        tracks, total_ms = snapshot_tracks(player)
        cap = track_cap_error(len(tracks), limits.max_playlist_tracks)
        if cap == "empty":
            await ctx.send(
                _("Nothing is playing - start some music before saving a playlist.")
            )
            return
        if cap == "too_many":
            await self._refuse_with_upsell(
                ctx,
                _(
                    "The queue is too long to save - a server playlist holds up "
                    "to {max} tracks. Trim it and try again."
                ).format(max=limits.max_playlist_tracks),
                limit_key="playlist_tracks",
                benefit=str(premium.GUILD_PREMIUM.max_playlist_tracks),
            )
            return

        if guild_cap_reached(
            await self._guild_playlist_count(ctx.guild.id), limits.max_guild_playlists
        ):
            await self._refuse_with_upsell(
                ctx,
                _(
                    "This server already has the maximum of {max} playlists. "
                    "Delete one first."
                ).format(max=limits.max_guild_playlists),
                limit_key="guild_playlists",
                benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
            )
            return

        result = await self._save_guild_playlist(
            ctx.guild.id,
            display,
            normalize_name(display),
            ctx.author.id,
            tracks,
            total_ms,
            limits.max_guild_playlists,
        )
        if result == "exists":
            await ctx.send(
                _(
                    "A playlist called **{name}** already exists here. Pick "
                    "another name or delete it first."
                ).format(name=echo_name(display)),
                allowed_mentions=NO_PINGS,
            )
            return
        if result == "full":
            await self._refuse_with_upsell(
                ctx,
                _(
                    "This server already has the maximum of {max} playlists. "
                    "Delete one first."
                ).format(max=limits.max_guild_playlists),
                limit_key="guild_playlists",
                benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
            )
            return

        from cogs.music.music import format_clock

        count = len(tracks)
        await ctx.send(
            ngettext(
                "Saved **{name}** with {count} track ({duration}).",
                "Saved **{name}** with {count} tracks ({duration}).",
                count,
            ).format(
                name=echo_name(display),
                count=count,
                duration=format_clock(total_ms),
            ),
            allowed_mentions=NO_PINGS,
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

        norm = normalize_name(name)
        row = await self._fetch_guild_playlist(ctx.guild.id, norm)
        if row is None:
            await ctx.send(
                _("There's no server playlist called **{name}**.").format(
                    name=echo_name(name)
                ),
                allowed_mentions=NO_PINGS,
            )
            return

        # Archived (M4a-1): over today's effective cap, either by count or by
        # this playlist's own track_count - refused before the decode round
        # trip is even spent. Delete still works (serverplaylist_delete below
        # carries no such check). Not sent ephemeral: ``ctx.defer()`` above
        # already committed this interaction to a PUBLIC response before the
        # row was even fetched, and a followup cannot retroactively turn
        # ephemeral - matches every other early refusal in this command.
        limits = self.bot.premium.for_guild(ctx.guild.id)
        archival = await self._guild_playlist_archival(
            ctx.guild.id, limits.max_guild_playlists
        )
        if playlist_is_archived(
            row["track_count"], limits.max_playlist_tracks, archival.is_active(norm)
        ):
            # Shares the "guild_playlists" key with the count/track-count caps
            # above - same limit, the archived symptom of it (the plan's own
            # rule for an archived-item refusal; see rolemenus.py's
            # archived-menu refusal for the same choice).
            await self._refuse_with_upsell(
                ctx,
                _(
                    "**{name}** is archived - this server is over its current "
                    "playlist limit. It can be deleted, but not loaded. See "
                    "/premium for options."
                ).format(name=echo_name(row["name"])),
                limit_key="guild_playlists",
                benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
                allowed_mentions=NO_PINGS,
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
                    name=echo_name(row["name"])
                ),
                allowed_mentions=NO_PINGS,
            )
            return

        player = await self._connect_for_playlist(ctx)
        if player is None:
            return

        # The per-guild queue cap decides here, at the enqueue seam - imported
        # lazily like ``format_clock`` above, because music.py imports THIS module
        # at load time, so a module-level import back would be a cycle. One bulk
        # decode is a single round trip (bounded by the guild's effective
        # max_playlist_tracks), so there
        # is nothing expensive to spare with an earlier refusal: queue the head
        # that fits and state the tail in the message below.
        from cogs.music.music import (
            fit_queue_additions,
            queue_full_message,
            queue_full_suffix,
            should_start_now,
        )

        usable, over_cap = fit_queue_additions(player.queue, usable)
        if not usable:
            await ctx.send(queue_full_message())
            return

        placed = await self._put_requested(player, usable, ctx.author)
        usable = placed.accepted
        if not usable:
            await ctx.send(placed.refusal() if placed.capped else queue_full_message())
            return

        # Loading a playlist is an explicit choice: it ends any radio session.
        player.radio_genre = None
        if should_start_now(player):
            # Owner decision: starting playback from idle (a user action) must
            # never inherit a stale paused flag. See resume_after_track_change.
            await player.play(player.queue.get(), paused=False)
        await self._snapshot(player)

        count = len(usable)
        message = ngettext(
            "Queued {count} track from **{name}**.",
            "Queued {count} tracks from **{name}**.",
            count,
        ).format(count=count, name=echo_name(row["name"]))
        if skipped:
            message += ngettext(
                " {skipped} track was skipped - it could not be loaded.",
                " {skipped} tracks were skipped - they could not be loaded.",
                skipped,
            ).format(skipped=skipped)
        message += queue_full_suffix(over_cap) + placed.suffix()
        await ctx.send(message, allowed_mentions=NO_PINGS)

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

        # Archived marking (M4a-1): classified from these SAME rows (every
        # playlist in the guild, already fetched) - no second query.
        limits = self.bot.premium.for_guild(ctx.guild.id)
        archival = classify_archival(
            (
                {"id": row["name_norm"], "created_at": row["created_at"]}
                for row in rows
            ),
            limits.max_guild_playlists,
        )
        prepared = [
            {
                "name": row["name"],
                "count": row["track_count"],
                "duration": format_clock(int(row["total_ms"] or 0)),
                "creator_id": row["creator_id"],
                "created_ts": int(row["created_at"].timestamp()),
                "archived": playlist_is_archived(
                    row["track_count"],
                    limits.max_playlist_tracks,
                    archival.is_active(row["name_norm"]),
                ),
                "link": link_line(row),
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
                    name=echo_name(name)
                ),
                allowed_mentions=NO_PINGS,
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
            _("Deleted the server playlist **{name}**.").format(
                name=echo_name(row["name"])
            ),
            allowed_mentions=NO_PINGS,
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
        old_norm = normalize_name(old)
        row = await self._fetch_guild_playlist(ctx.guild.id, old_norm)
        if row is None:
            await ctx.send(
                _("There's no server playlist called **{name}**.").format(
                    name=echo_name(old)
                ),
                allowed_mentions=NO_PINGS,
            )
            return
        if not can_manage(
            ctx.author, row["creator_id"], self._has_manage_guild(ctx.author)
        ):
            await ctx.send(
                _("Only the playlist's creator or a moderator can do that.")
            )
            return

        # Archived (M4a-1): renaming is an edit, refused the same way loading
        # one is - see serverplaylist_play's own archived check. This command
        # never defers, so the refusal CAN be ephemeral on the slash surface.
        limits = self.bot.premium.for_guild(ctx.guild.id)
        archival = await self._guild_playlist_archival(
            ctx.guild.id, limits.max_guild_playlists
        )
        if playlist_is_archived(
            row["track_count"], limits.max_playlist_tracks, archival.is_active(old_norm)
        ):
            text = _(
                "**{name}** is archived - this server is over its current "
                "playlist limit. It can be deleted, but not renamed. See "
                "/premium for options."
            ).format(name=echo_name(row["name"]))
            # Same "guild_playlists" key as the serverplaylist_play archived
            # refusal - this command never defers, so (unlike that one) the
            # base refusal IS already ephemeral on slash, which means the
            # upsell (and its button) can ride the SAME message instead of a
            # separate followup.
            is_slash = premium_upsell.is_slash_context(ctx)
            upsell = await premium_upsell.for_guild_refusal(
                self.bot,
                limit_key="guild_playlists",
                guild_id=ctx.guild.id,
                person_id=ctx.author.id,
                is_admin=premium_upsell.invoker_is_admin(ctx.author),
                already_top_tier=premium_upsell.is_guild_already_top_tier(
                    self.bot, ctx.guild.id
                ),
                benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
                allow_button=is_slash,
            )
            send_kwargs = {
                "ephemeral": ctx.interaction is not None,
                "allowed_mentions": NO_PINGS,
            }
            if upsell is not None:
                text = text + "\n" + upsell.line
                view = upsell.view()
                if view is not None:
                    send_kwargs["view"] = view
            await ctx.send(text, **send_kwargs)
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
                    ).format(name=echo_name(new_display)),
                    allowed_mentions=NO_PINGS,
                )
                return

        await self._rename_guild_playlist(
            ctx.guild.id, old_norm, new_display, new_norm
        )
        await ctx.send(
            _("Renamed **{old}** to **{new}**.").format(
                old=echo_name(row["name"]), new=echo_name(new_display)
            ),
            allowed_mentions=NO_PINGS,
        )

    @serverplaylist_rename.autocomplete("old")
    async def _serverplaylist_rename_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice[str]]:
        """Reuse the play autocomplete for the rename ``old`` argument."""
        return await self._serverplaylist_play_autocomplete(interaction, current)

    # -- Import from a link / unlink --------------------------------------

    def _import_summary(
        self, display: str, count: int, total_ms: int, over: int, linked_domain
    ) -> str:
        """The success line of an import: counts, the clipped tail, the link."""
        from cogs.music.music import format_clock

        text = ngettext(
            "Imported **{name}** with {count} track ({duration}).",
            "Imported **{name}** with {count} tracks ({duration}).",
            count,
        ).format(
            name=echo_name(display), count=count, duration=format_clock(total_ms)
        )
        if over:
            text += ngettext(
                " {over} track was over the playlist limit and left out.",
                " {over} tracks were over the playlist limit and left out.",
                over,
            ).format(over=over)
        if linked_domain:
            text += " " + _(
                "It is linked to {domain} and refreshes every week."
            ).format(domain=linked_domain)
        return text

    @serverplaylist.command(name="import")
    @commands.guild_only()
    @app_commands.describe(
        url="A public playlist link (YouTube, Spotify, Deezer, Apple Music...).",
        name="A name for the server playlist.",
        keep_synced="Yasuho+: refresh the playlist from the link every week.",
    )
    async def serverplaylist_import(
        self,
        ctx: commands.Context,
        url: str,
        *,
        name: str,
        keep_synced: bool = False,
    ) -> None:
        """Save a playlist link as a server playlist (optionally kept in sync).

        ``keep_synced`` is declared AFTER the consume-rest ``name``: on the
        prefix surface it is never parsed and keeps its default, exactly like
        ``remind``'s ``repeat``.
        """
        # Every import is a Lavalink (and maybe Spotify) load: rate-limit per
        # user and per guild BEFORE spending anything, refusing privately.
        quotas = self.quotas
        if not quotas.import_user.check(ctx.author.id):
            delay = self._format_retry_delay(quotas.import_user.retry_after(ctx.author.id))
            await ctx.send(
                _("You're importing too fast. Try again in {delay}.").format(delay=delay),
                ephemeral=True,
            )
            return
        if not quotas.import_guild.check(ctx.guild.id):
            delay = self._format_retry_delay(quotas.import_guild.retry_after(ctx.guild.id))
            await ctx.send(
                _("This server has imported too many playlists lately. Try again in {delay}.").format(
                    delay=delay
                ),
                ephemeral=True,
            )
            return

        # A Lavalink load can take seconds: acknowledge before anything slow.
        await ctx.defer()

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
        norm = normalize_name(display)
        limits = self.bot.premium.for_guild(ctx.guild.id)

        if keep_synced:
            if not self._has_manage_guild(ctx.author):
                await ctx.send(
                    _("Only moderators can keep a playlist synced with a link.")
                )
                return
            if limits.max_linked_playlists <= 0:
                await self._refuse_with_upsell(
                    ctx,
                    _(
                        "Keeping a playlist synced with its link is a Yasuho+ "
                        "feature. You can still import it without `keep_synced`."
                    ),
                    limit_key="linked_playlists",
                    benefit=str(premium.GUILD_PREMIUM.max_linked_playlists),
                    kind="unlock",
                )
                return

        if not self._nodes_available():
            await ctx.send(
                _("Music is currently unavailable - no Lavalink node is connected.")
            )
            return

        row = await self._fetch_guild_playlist(ctx.guild.id, norm)
        if row is not None:
            if not can_manage(
                ctx.author, row["creator_id"], self._has_manage_guild(ctx.author)
            ):
                await ctx.send(
                    _("Only the playlist's creator or a moderator can do that.")
                )
                return
            archival = await self._guild_playlist_archival(
                ctx.guild.id, limits.max_guild_playlists
            )
            if playlist_is_archived(
                row["track_count"], limits.max_playlist_tracks, archival.is_active(norm)
            ):
                await self._refuse_with_upsell(
                    ctx,
                    _(
                        "**{name}** is archived - this server is over its current "
                        "playlist limit. It can be deleted, but not replaced. See "
                        "/premium for options."
                    ).format(name=echo_name(row["name"])),
                    limit_key="guild_playlists",
                    benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
                    allowed_mentions=NO_PINGS,
                )
                return
        elif guild_cap_reached(
            await self._guild_playlist_count(ctx.guild.id), limits.max_guild_playlists
        ):
            await self._refuse_with_upsell(
                ctx,
                _(
                    "This server already has the maximum of {max} playlists. "
                    "Delete one first."
                ).format(max=limits.max_guild_playlists),
                limit_key="guild_playlists",
                benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
            )
            return

        if keep_synced:
            already = 1 if (row is not None and row["source_url"]) else 0
            if await self._linked_count(ctx.guild.id) - already >= (
                limits.max_linked_playlists
            ):
                await self._refuse_with_upsell(
                    ctx,
                    _(
                        "This server already has the maximum of {max} linked "
                        "playlists. Unlink one first."
                    ).format(max=limits.max_linked_playlists),
                    limit_key="linked_playlists",
                    benefit=str(premium.GUILD_PREMIUM.max_linked_playlists),
                )
                return

        # Charge both axes for the load we are about to make (a failing link
        # still costs the node a round trip).
        quotas.import_user.hit(ctx.author.id)
        quotas.import_guild.hit(ctx.guild.id)
        source = await self._fetch_source_tracks(url)
        refusal = {
            playlist_sync.BLOCKED: _(
                "I can only play public links - I can't reach that one."
            ),
            playlist_sync.BAD: _("That link isn't a playlist I can import."),
            playlist_sync.NOT_PLAYLIST: _("That link isn't a playlist I can import."),
            playlist_sync.UNAVAILABLE: _(
                "Music is currently unavailable - no Lavalink node is connected."
            ),
            playlist_sync.EMPTY: _("That playlist has no tracks I can import."),
            playlist_sync.LOAD_FAILED: _(
                "I couldn't load that link - it may be private or unavailable."
            ),
            playlist_sync.TRANSIENT: _(
                "I couldn't load that link - it may be private or unavailable."
            ),
        }.get(source.status)
        if refusal is not None:
            await ctx.send(refusal)
            return

        source, over = playlist_sync.clip_result(source, limits.max_playlist_tracks)
        source_url = url.strip() if keep_synced else None
        next_sync_at = (
            playlist_sync.next_due(discord.utils.utcnow(), jitter=random.random())
            if keep_synced
            else None
        )
        domain = playlist_sync.source_domain(source_url) if keep_synced else None
        count = len(source.blobs)
        summary = self._import_summary(display, count, source.total_ms, over, domain)

        if row is None:
            result = await self._import_new_playlist(
                ctx.guild.id,
                display,
                norm,
                ctx.author.id,
                source.blobs,
                source.total_ms,
                limits.max_guild_playlists,
                source_url=source_url,
                next_sync_at=next_sync_at,
                max_linked=limits.max_linked_playlists,
            )
            if result == "exists":
                await ctx.send(
                    _(
                        "A playlist called **{name}** already exists here. Pick "
                        "another name or delete it first."
                    ).format(name=echo_name(display)),
                    allowed_mentions=NO_PINGS,
                )
            elif result == "link_full":
                await self._refuse_with_upsell(
                    ctx,
                    _(
                        "This server already has the maximum of {max} linked "
                        "playlists. Unlink one first."
                    ).format(max=limits.max_linked_playlists),
                    limit_key="linked_playlists",
                    benefit=str(premium.GUILD_PREMIUM.max_linked_playlists),
                )
            elif result == "full":
                await self._refuse_with_upsell(
                    ctx,
                    _(
                        "This server already has the maximum of {max} playlists. "
                        "Delete one first."
                    ).format(max=limits.max_guild_playlists),
                    limit_key="guild_playlists",
                    benefit=str(premium.GUILD_PREMIUM.max_guild_playlists),
                )
            else:
                await ctx.send(summary, allowed_mentions=NO_PINGS)
            return

        # The name is taken: replacing it is destructive, so ask first.
        plan = {
            "tracks": source.blobs,
            "total_ms": source.total_ms,
            "source_url": source_url,
            "next_sync_at": next_sync_at,
            "max_linked": limits.max_linked_playlists,
            "expect_creator": row["creator_id"],
            "expect_created": row["created_at"],
        }
        view = _ReplaceImportView(self, ctx.author, ctx.guild.id, norm, plan)
        view.message = await ctx.send(
            ngettext(
                "**{name}** already exists. Replace it with the {count} track "
                "from this link?",
                "**{name}** already exists. Replace it with the {count} tracks "
                "from this link?",
                count,
            ).format(name=echo_name(row["name"]), count=count),
            view=view,
            allowed_mentions=NO_PINGS,
        )

    @serverplaylist.command(name="unlink")
    @commands.guild_only()
    @app_commands.describe(name="Which linked server playlist to unlink.")
    async def serverplaylist_unlink(
        self, ctx: commands.Context, *, name: str
    ) -> None:
        """Stop syncing a playlist with its link (its tracks are kept)."""
        norm = normalize_name(name)
        row = await self._fetch_guild_playlist(ctx.guild.id, norm)
        if row is None:
            await ctx.send(
                _("There's no server playlist called **{name}**.").format(
                    name=echo_name(name)
                ),
                allowed_mentions=NO_PINGS,
            )
            return
        if not can_manage(
            ctx.author, row["creator_id"], self._has_manage_guild(ctx.author)
        ):
            await ctx.send(
                _("Only the playlist's creator or a moderator can do that.")
            )
            return
        if not await self._unlink_playlist(ctx.guild.id, norm):
            await ctx.send(
                _("**{name}** isn't linked to a source.").format(
                    name=echo_name(row["name"])
                ),
                allowed_mentions=NO_PINGS,
            )
            return
        await ctx.send(
            _("**{name}** is no longer linked; it keeps its current tracks.").format(
                name=echo_name(row["name"])
            ),
            allowed_mentions=NO_PINGS,
        )

    @serverplaylist_unlink.autocomplete("name")
    async def _serverplaylist_unlink_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> typing.List[app_commands.Choice[str]]:
        """Reuse the play autocomplete for the unlink name argument."""
        return await self._serverplaylist_play_autocomplete(interaction, current)
