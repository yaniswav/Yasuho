"""Democratic skip votes for the music player: scaled, one public vote per guild.

When a non-privileged listener asks to skip, a plain skip would let any single
person cut the room's track. Instead this module runs a lightweight majority
vote: it posts ONE public message in the music channel with a live vote button,
and the track only advances once enough of the humans in the voice channel have
agreed. The DJ and Manage-Server members are exempt (the cog decides that and
skips instantly); a room of two or fewer humans skips instantly too (a 1-of-1
vote is theatre).

Layering mirrors ``lyrics.py`` / ``effects.py``: this is a LEAF the cog imports.

* The decision maths (:func:`count_humans`, :func:`required_votes`,
  :func:`skip_mode`) and the per-vote bookkeeping (:meth:`SkipVote.record`) are
  **pure** - no discord, no sonolink, no i18n behind the caller's back - so they
  unit-test without any backend.
* The Discord surface (the public vote message, its one button) and the bounded
  per-guild registry live here too, duck-typing the ``player`` and the ``cog``
  (it calls back into ``cog._execute_skip`` for the actual advance), so this
  module imports NO sonolink and imports identically under the stub on the dev
  box, and never forms an import cycle with ``music.py`` / ``views.py``.

Scale. State is one dict entry per guild that currently has a live vote, each
holding a set of voter ids bounded by the voice channel's human count (Discord
caps that at 99) and a single Discord message whose 30 s timeout is managed by
discord.py itself - there is NO extra background task and NO database. A vote
self-detaches on pass / expiry / track change / player teardown, and the
registry also lazily sweeps any resolved vote past a size cap, so the map is
bounded by the number of guilds voting *right now*, never by guild count.
:meth:`SkipVote.record` is O(1) (a set add and two length compares); the live
threshold recount walks only the tiny, bounded channel-member list.
"""

from __future__ import annotations

import logging
import typing

import discord

from tools import interactions
from tools.i18n import _

log = logging.getLogger(__name__)


# How long (seconds) a live vote stays open with no further button votes before
# it expires. discord.py's View owns this timer (it refreshes on each button
# interaction), so an actively-voting room keeps the vote alive while a stalled
# one finalises 30 s after the last click - no custom loop needed.
VOTE_TTL = 30.0

# A room of this many humans or fewer skips instantly instead of voting: a
# threshold of 1 (a lone yes) is not a vote worth posting.
INSTANT_MAX_HUMANS = 2


# skip_mode results: the cog's two branches.
SKIP_INSTANT = "instant"  # caller performs its normal (instant) skip
SKIP_VOTE = "vote"  # a public vote should run

# Vote-record outcomes (also the ack keys the cog surfaces render).
VOTE_OPENED = "opened"  # a brand-new vote was posted
VOTE_COUNTED = "counted"  # the vote was tallied, threshold not yet reached
VOTE_ALREADY = "already"  # this member had already voted (or the vote is closed)
VOTE_PASSED = "passed"  # this vote reached the threshold; the skip was performed
VOTE_ENDED = "ended"  # the voted-on track had already changed; vote self-cancelled

# cog._execute_skip results, shared so this module can read the resolution
# outcome without importing sonolink.
SKIP_RESULT_NONE = "none"  # nothing to skip to; playback left untouched
SKIP_RESULT_ADVANCED = "advanced"  # skipped onto a new track
SKIP_RESULT_ENDED = "ended"  # skip emptied the queue; state was cleared


# ---------------------------------------------------------------------------
# Pure decision maths.
# ---------------------------------------------------------------------------


def count_humans(members: typing.Iterable[typing.Any]) -> int:
    """Count the non-bot members in ``members`` (bots never vote and never count).

    Pure and None-safe over the member shapes the fakes mirror (anything whose
    ``bot`` attribute is truthy is excluded).
    """
    return sum(1 for member in members if not getattr(member, "bot", False))


def required_votes(humans: int) -> int:
    """Votes needed to carry a skip: ``ceil(humans / 2)`` (a simple majority).

    ``(humans + 1) // 2`` equals ``ceil(humans / 2)`` for every non-negative
    integer, so 3 humans need 2, 4 need 2, 8 need 4. A room that has shrunk to 2
    needs 1 and to 1 needs 1, which is why the live recount naturally resolves a
    vote the moment the room drops to instant-skip size. Pure.
    """
    return (max(humans, 0) + 1) // 2


def skip_mode(humans: int, *, exempt: bool) -> str:
    """Decide whether a skip request skips instantly or opens a vote.

    Returns :data:`SKIP_INSTANT` for a privileged actor (``exempt`` - the DJ or a
    Manage-Server member, decided by the cog) or a room of
    :data:`INSTANT_MAX_HUMANS` humans or fewer; otherwise :data:`SKIP_VOTE`. Pure,
    so the whole instant-vs-vote decision is unit-tested without a player.
    """
    if exempt:
        return SKIP_INSTANT
    if humans <= INSTANT_MAX_HUMANS:
        return SKIP_INSTANT
    return SKIP_VOTE


def skip_ack(outcome: str) -> str:
    """Translate a vote-record ``outcome`` into a short ephemeral ack line.

    Called in-task (never at import) so ``_()`` resolves against the caller's
    locale. The strings deliberately reuse the finalise/ephemeral message ids so
    the vote speaks with one voice across surfaces.
    """
    if outcome == VOTE_OPENED:
        return _("Started a vote to skip.")
    if outcome == VOTE_COUNTED:
        return _("Added your vote to skip.")
    if outcome == VOTE_PASSED:
        return _("Skipped by vote.")
    if outcome == VOTE_ENDED:
        return _("This track already ended.")
    return _("You already voted to skip.")


def _vote_label(count: int, needed: int) -> str:
    """Render the vote button's live-count label (``Vote skip (1/3)``).

    In-task only. The ``count/needed`` fraction is a numeric ratio, not
    pluralised prose, so this needs no ngettext.
    """
    return _("Vote skip ({count}/{needed})").format(count=count, needed=needed)


def _in_players_voice(player: typing.Any, member: typing.Any) -> bool:
    """True when ``member`` is currently in ``player``'s voice channel.

    The vote message posts publicly, so - like the now-playing controller and the
    synced-lyrics card - only listeners actually in the channel may cast a vote.
    Duck-typed so this module needs no import from the cog.
    """
    channel = getattr(player, "channel", None)
    if channel is None:
        return False
    voice = getattr(member, "voice", None)
    return voice is not None and getattr(voice, "channel", None) == channel


def _guild_id_of(player: typing.Any) -> typing.Optional[int]:
    """Return the player's guild id, or None if it cannot be resolved."""
    guild = getattr(player, "guild", None)
    if guild is None:
        guild = getattr(getattr(player, "channel", None), "guild", None)
    return getattr(guild, "id", None)


# ---------------------------------------------------------------------------
# Discord surface: the one public vote message and its button.
# ---------------------------------------------------------------------------


class _VoteButton(discord.ui.Button):
    """The single Vote button on a live skip-vote message.

    Its label carries the live count (``Vote skip (2/3)``). A click same-voice
    gates the clicker, records their vote once, then either refreshes the count
    in place, finalises the message on a pass / stale track, or replies
    ephemerally that they already voted.
    """

    def __init__(self, vote: "SkipVote") -> None:
        self._vote = vote
        super().__init__(
            style=discord.ButtonStyle.primary,
            emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}",
            label=_vote_label(vote.count(), vote.required()),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            vote = self._vote
            if not _in_players_voice(vote.player, interaction.user):
                await interaction.response.send_message(
                    _("You must be in my voice channel to use these controls."),
                    ephemeral=True,
                )
                return
            outcome = vote.record(interaction.user.id)
            if outcome == VOTE_ALREADY:
                await interaction.response.send_message(
                    _("You already voted to skip."), ephemeral=True
                )
                return
            # COUNTED / PASSED / ENDED: ack the click silently (the message edit
            # apply() makes is the feedback everyone sees), then update in place.
            await interaction.response.defer()
            await vote.apply(outcome)
        except Exception:
            log.exception("Skip-vote button failed")
            await interactions.notify_failure(interaction)


class SkipVoteView(discord.ui.View):
    """The vote message's view: one button, a 30 s timeout, no author gate.

    Multi-user by nature (any listener in the channel may vote), so it is a plain
    :class:`discord.ui.View`, not an author-locked one. On timeout it hands back
    to the vote to finalise ("Vote expired.").
    """

    def __init__(self, vote: "SkipVote", *, timeout: float = VOTE_TTL) -> None:
        super().__init__(timeout=timeout)
        self._vote = vote
        self._button = _VoteButton(vote)
        self.add_item(self._button)

    def set_count(self, count: int, needed: int) -> None:
        """Refresh the button's live-count label."""
        self._button.label = _vote_label(count, needed)

    def disable(self) -> None:
        """Disable the button (used when the vote finalises)."""
        self._button.disabled = True

    async def on_timeout(self) -> None:
        await self._vote.expire()


class SkipVote:
    """One live skip vote for a single guild.

    Holds the voter-id set (the initiator is seeded as the first vote), the
    identity of the track being voted on (so a vote for a track that has since
    changed self-cancels), and the one public message it edits in place. The
    threshold is recomputed against the CURRENT humans in the channel on every
    vote, so a room that shrinks lowers the bar on the next click; voters who
    leave keep their counted vote (a documented, deliberate simplicity - no
    presence revalidation).

    All discord objects are created in :meth:`start`, so :meth:`record` and the
    decision maths stay loop-free and unit-testable without a running loop.
    """

    def __init__(
        self,
        *,
        cog: typing.Any,
        player: typing.Any,
        channel: typing.Any,
        track: typing.Any,
        initiator: typing.Any,
        registry: "SkipVotes",
        guild_id: int,
        timeout: float = VOTE_TTL,
    ) -> None:
        self.cog = cog
        self.player = player
        self.channel = channel
        self.guild_id = guild_id
        self.track_id = getattr(track, "identifier", None)
        self._track_title = (getattr(track, "title", "") or "")[:256]
        self._initiator_mention = getattr(initiator, "mention", "")
        self.votes: typing.Set[int] = {getattr(initiator, "id", 0)}
        self._registry = registry
        self._timeout = timeout
        self.message: typing.Optional[discord.Message] = None
        self._view: typing.Optional[SkipVoteView] = None
        self._resolved = False

    # -- state --------------------------------------------------------------

    @property
    def resolved(self) -> bool:
        return self._resolved

    def count(self) -> int:
        """How many members have voted so far (O(1))."""
        return len(self.votes)

    def required(self) -> int:
        """Votes needed right now, against the CURRENT human count in the channel."""
        members = getattr(getattr(self.player, "channel", None), "members", ())
        return required_votes(count_humans(members))

    def matches(self, track: typing.Any) -> bool:
        """True when ``track`` is still the track this vote was opened for."""
        return getattr(track, "identifier", None) == self.track_id

    def record(self, member_id: int) -> str:
        """Tally one member's vote and classify the result (pure bookkeeping, O(1)).

        Returns :data:`VOTE_ALREADY` when the vote is closed or the member had
        already voted, :data:`VOTE_ENDED` when the voted-on track has changed out
        from under the vote, :data:`VOTE_PASSED` when this vote reaches the live
        threshold, else :data:`VOTE_COUNTED`. No discord, no I/O - the async
        follow-through lives in :meth:`apply`.
        """
        if self._resolved:
            return VOTE_ALREADY
        if not self.matches(getattr(self.player, "current", None)):
            return VOTE_ENDED
        if member_id in self.votes:
            return VOTE_ALREADY
        self.votes.add(member_id)
        if self.count() >= self.required():
            return VOTE_PASSED
        return VOTE_COUNTED

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Post the public vote message with the live button. Raises on send error.

        The caller (the registry) catches an HTTP error, detaches the vote and
        degrades to an instant skip - a room with no postable channel should not
        be stuck unable to skip.
        """
        self._view = SkipVoteView(self, timeout=self._timeout)
        content = _("{user} wants to skip **{title}**.").format(
            user=self._initiator_mention, title=self._track_title
        )
        self.message = await self.channel.send(
            content=content,
            view=self._view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def apply(self, outcome: str) -> None:
        """Carry out the async follow-through for a :meth:`record` outcome."""
        if outcome == VOTE_COUNTED:
            await self._update_count()
        elif outcome == VOTE_PASSED:
            await self._resolve()
        elif outcome == VOTE_ENDED:
            await self.cancel(_("This track already ended."))
        # VOTE_ALREADY: nothing on screen changes.

    async def expire(self) -> None:
        """View-timeout handler: finalise the message as expired."""
        await self.cancel(_("Vote expired."))

    async def cancel(self, text: str) -> None:
        """Finalise a still-live vote with ``text`` (idempotent).

        The shared external-stop path: a track change, the on-track-start hook and
        the cog's teardown all land here. A no-op once the vote has resolved.
        """
        if self._resolved:
            return
        self._resolved = True
        self._registry._detach(self.guild_id)
        await self._write_final(text)

    async def _resolve(self) -> None:
        """The threshold was reached: perform the skip, then finalise the message.

        Detaches FIRST so the track_start our own skip fires cannot re-enter and
        double-finalise (the hook / clear then find no live vote). Routes the
        advance through the EXACT engine the /skip command uses (``can_skip``
        precheck included); if nothing can be skipped to, the vote finalises with
        the command's own refusal line instead.
        """
        self._resolved = True
        self._registry._detach(self.guild_id)
        result, _track = await self.cog._execute_skip(self.player)
        if result == SKIP_RESULT_NONE:
            text = _("There are no more tracks in the queue to skip to.")
        else:
            text = _("Skipped by vote.")
        await self._write_final(text)

    async def _update_count(self) -> None:
        """Edit the button's live count in place (view-only edit, no content churn)."""
        if self._view is None or self.message is None:
            return
        self._view.set_count(self.count(), self.required())
        try:
            await self.message.edit(view=self._view)
        except discord.HTTPException:
            log.exception("Failed to refresh skip-vote message for guild %s", self.guild_id)

    async def _write_final(self, text: str) -> None:
        """Disable the button and replace the message content with ``text``."""
        if self._view is not None:
            self._view.disable()
            self._view.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(content=text, view=self._view)
        except discord.HTTPException:
            log.exception("Failed to finalise skip-vote message for guild %s", self.guild_id)


# ---------------------------------------------------------------------------
# Bounded per-cog registry: at most one live vote per guild.
# ---------------------------------------------------------------------------


class SkipVotes:
    """The live skip votes, at most one per guild, bounded by a lazy sweep.

    A fresh request for a guild whose vote is stale (resolved or for a track that
    has since changed) replaces it, so there is never more than one live message
    per guild. Each vote self-detaches on pass / expiry / track change / teardown;
    :meth:`_put` also sweeps any resolved vote once the map grows past
    ``sweep_at``, so the map is bounded by the number of guilds voting right now -
    the same discipline as the lyrics sessions and the pending-voice watches.
    """

    def __init__(self, *, sweep_at: int = 256) -> None:
        self._sweep_at = sweep_at
        self._votes: typing.Dict[int, SkipVote] = {}

    def get(self, guild_id: int) -> typing.Optional[SkipVote]:
        return self._votes.get(guild_id)

    def count(self) -> int:
        return len(self._votes)

    def _put(self, guild_id: int, vote: SkipVote) -> None:
        self._votes[guild_id] = vote
        if len(self._votes) > self._sweep_at:
            self._sweep()

    def _sweep(self) -> None:
        """Drop any resolved vote (self-detach should have, this is a safety net)."""
        self._votes = {
            gid: vote for gid, vote in self._votes.items() if not vote.resolved
        }

    def _detach(self, guild_id: int) -> None:
        """Forget a guild's vote (idempotent)."""
        self._votes.pop(guild_id, None)

    async def open(
        self,
        cog: typing.Any,
        player: typing.Any,
        initiator: typing.Any,
        fallback_channel: typing.Any,
    ) -> str:
        """Open a new vote, or add ``initiator`` to the live one for their guild.

        Returns a vote-record outcome (or :data:`VOTE_OPENED` for a brand-new
        vote) for the caller to ack ephemerally, or :data:`SKIP_INSTANT` when it
        could not run a vote (no guild, no postable channel, or the room shrank to
        instant-skip size) and the caller should skip instantly instead.
        """
        guild_id = _guild_id_of(player)
        if guild_id is None:
            return SKIP_INSTANT
        channel = getattr(player, "home", None) or fallback_channel
        if channel is None:
            return SKIP_INSTANT

        track = getattr(player, "current", None)
        existing = self._votes.get(guild_id)
        if existing is not None and not existing.resolved:
            if existing.matches(track):
                outcome = existing.record(getattr(initiator, "id", 0))
                await existing.apply(outcome)
                return outcome
            # A live vote for a track that has already changed (the track_start
            # hook has not fired yet): finalise it before opening a fresh one, so
            # its message never orphans with a still-active button.
            await existing.cancel(_("This track already ended."))

        # No live vote for this guild's current track. Re-read the live human
        # count: if the room shrank since the cog's decision so a lone vote would
        # already clear the bar, skip instantly rather than post a 1-of-1 vote.
        humans = count_humans(getattr(getattr(player, "channel", None), "members", ()))
        if required_votes(humans) <= 1:
            return SKIP_INSTANT

        vote = SkipVote(
            cog=cog,
            player=player,
            channel=channel,
            track=track,
            initiator=initiator,
            registry=self,
            guild_id=guild_id,
        )
        self._put(guild_id, vote)
        try:
            await vote.start()
        except discord.HTTPException:
            log.exception("Failed to post skip-vote message for guild %s", guild_id)
            self._detach(guild_id)
            return SKIP_INSTANT
        return VOTE_OPENED

    async def notify_track(
        self, guild_id: int, track_id: typing.Optional[str]
    ) -> None:
        """A track_start fired: cancel a live vote whose track actually changed.

        A reconnect re-fires track_start for the SAME track (ids match -> the vote
        stays); a genuine next-track / natural end changes the id, so the stale
        vote finalises proactively rather than lingering its full timeout.
        """
        vote = self._votes.get(guild_id)
        if vote is not None and not vote.resolved and vote.track_id != track_id:
            await vote.cancel(_("This track already ended."))

    async def clear(self, guild_id: int) -> None:
        """Cancel and forget a guild's vote on player teardown (idempotent)."""
        vote = self._votes.pop(guild_id, None)
        if vote is not None and not vote.resolved:
            await vote.cancel(_("This track already ended."))
