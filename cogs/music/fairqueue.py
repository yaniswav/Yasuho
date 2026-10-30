"""Fair queue: where a newly requested track lands in the user lane.

Plain FIFO lets one member who queues forty tracks bury everybody else. With the
server's fair queue ON, each new track is inserted so the user lane interleaves
requesters round-robin (A1 B1 C1 A2 B2 ...); with it OFF (the default) none of
this runs and the queue behaves exactly as it always did.

A leaf module: pure functions over a list of tracks plus one small plan object,
so it is unit-testable with no Discord, no sonolink and no database.

Rules
-----
* The requester of a track is ``track.extras.requester`` (set by every enqueue
  seam). A track with no requester forms one group of its own.
* A track's ROUND is how many tracks by the same requester precede it in the
  lane. A new track by X has round = X's tracks already queued, and goes in
  before the first track of a later round, but never before one of X's own
  tracks (so a lane that held FIFO content from before the mode was switched on
  is never reordered, only inserted into).
* DJ priority (Yasuho+): a track queued by a music manager is flagged
  (``extras.dj_priority``) and goes at the end of the leading block of flagged
  tracks, i.e. managers first, FIFO among themselves. Everyone else interleaves
  behind that block.
* Only the user lane is ever read or written. The autoplay lane and the 24/7
  playlist never pass through here.

Scale: an insert is O(queue); the queue is capped at ``MAX_QUEUE_TRACKS`` (500)
so the worst case is a few hundred list operations. No I/O, no timers.
"""

from __future__ import annotations

import dataclasses
import typing

from tools.i18n import ngettext

DJ_FLAG = "dj_priority"


def requester_of(track: typing.Any) -> typing.Optional[int]:
    return getattr(getattr(track, "extras", None), "requester", None)


def is_anonymous(track: typing.Any) -> bool:
    """A track that takes no part in rounds or caps.

    No requester (a cold-restored track keeps only its encoded blob) or a radio /
    24/7 playlist track (its "requester" is just whoever started the station).
    """
    extras = getattr(track, "extras", None)
    return (
        requester_of(track) is None
        or bool(getattr(extras, "radio", False))
        or bool(getattr(extras, "radio247", False))
    )


def is_flagged(track: typing.Any) -> bool:
    return bool(getattr(getattr(track, "extras", None), DJ_FLAG, False))


@dataclasses.dataclass(frozen=True)
class QueuePlan:
    """What the guild asked of this enqueue (``None`` plan = legacy FIFO)."""

    fair: bool = False
    dj_priority: bool = False
    member_cap: typing.Optional[int] = None
    # Whether THIS actor is a music manager (exempt from the cap, flagged when
    # dj_priority is on).
    manager: bool = False


@dataclasses.dataclass(frozen=True)
class Placed:
    """Outcome of one enqueue: what landed, and what the member cap turned away."""

    accepted: typing.List[typing.Any]
    capped: int = 0
    cap: typing.Optional[int] = None
    # Tracks the queue-room re-check dropped after the caller's own check.
    dropped: int = 0
    queue_cap: int = 0

    def refusal(self) -> str:
        """Ephemeral text when NOTHING landed because of the cap."""
        return ngettext(
            "You already have the maximum of {cap} track in the queue. Wait for"
            " one to play first.",
            "You already have the maximum of {cap} tracks in the queue. Wait for"
            " one to play first.",
            self.cap or 0,
        ).format(cap=self.cap)

    def suffix(self) -> str:
        """Tail for a bulk add the cap or the room re-check cut short."""
        out = ""
        if self.dropped > 0:
            out += ngettext(
                " {dropped} track was not added - the queue is full at {cap}.",
                " {dropped} tracks were not added - the queue is full at {cap}.",
                self.dropped,
            ).format(dropped=self.dropped, cap=self.queue_cap)
        if self.capped > 0:
            out += ngettext(
                " {dropped} track was not added - the limit is {cap} per member.",
                " {dropped} tracks were not added - the limit is {cap} per member.",
                self.capped,
            ).format(dropped=self.capped, cap=self.cap)
        return out


def count_queued_by(lane: typing.Iterable[typing.Any], requester_id: typing.Any) -> int:
    """How many tracks of ``lane`` were requested by ``requester_id``."""
    return sum(
        1
        for track in lane
        if requester_of(track) == requester_id and not is_anonymous(track)
    )


def apply_member_cap(
    lane: typing.Sequence[typing.Any],
    tracks: typing.Sequence[typing.Any],
    requester_id: typing.Any,
    cap: typing.Optional[int],
) -> typing.Tuple[typing.List[typing.Any], int]:
    """Split ``tracks`` into ``(accepted, refused)`` against a per-member cap."""
    tracks = list(tracks)
    if cap is None:
        return tracks, 0
    room = max(0, cap - count_queued_by(lane, requester_id))
    return tracks[:room], max(0, len(tracks) - room)


def plan_inserts(
    lane: typing.Sequence[typing.Any],
    tracks: typing.Sequence[typing.Any],
    requester_id: typing.Any,
    *,
    flagged: bool = False,
) -> typing.List[typing.Tuple[int, typing.Any]]:
    """Return ``[(index, track), ...]`` to apply IN ORDER to the live lane.

    Each index is valid at the moment its insert is applied (the earlier inserts
    of the same call have already landed), so a caller does
    ``put_at(index, track)`` sequentially. ``flagged`` marks the tracks as a
    manager's DJ-priority additions. Pure: ``lane`` is not modified.
    """
    reqs = [requester_of(t) for t in lane]
    anon = [is_anonymous(t) for t in lane]
    flags = [is_flagged(t) for t in lane]
    # Rounds count only tracks that belong to a requester.
    rounds: typing.List[int] = []
    counts: typing.Dict[typing.Any, int] = {}
    for req, skip in zip(reqs, anon):
        if skip:
            rounds.append(0)
            continue
        rounds.append(counts.get(req, 0))
        counts[req] = counts.get(req, 0) + 1
    # End of the leading flagged block.
    front = 0
    while front < len(flags) and flags[front]:
        front += 1

    out: typing.List[typing.Tuple[int, typing.Any]] = []
    for track in tracks:
        if flagged:
            index = front
            front += 1
            reqs.insert(index, requester_id)
            anon.insert(index, False)
            rounds.insert(index, 0)
            flags.insert(index, True)
        else:
            k = counts.get(requester_id, 0)
            # Never interleave past a requester-less track: start after the last.
            base = front
            for i in range(len(anon) - 1, front - 1, -1):
                if anon[i]:
                    base = i + 1
                    break
            last_own = -1
            for i in range(len(reqs) - 1, base - 1, -1):
                if reqs[i] == requester_id and not anon[i]:
                    last_own = i
                    break
            index = len(reqs)
            for i in range(base, len(reqs)):
                if rounds[i] > k:
                    index = i
                    break
            index = max(index, last_own + 1, base)
            reqs.insert(index, requester_id)
            anon.insert(index, False)
            rounds.insert(index, k)
            flags.insert(index, False)
            counts[requester_id] = k + 1
        out.append((index, track))
    return out


def place(queue: typing.Any, tracks: typing.Sequence[typing.Any], requester_id: typing.Any, plan: QueuePlan) -> None:
    """Insert ``tracks`` into ``queue``'s user lane according to ``plan``.

    Sets the DJ flag when the plan prioritises this (manager) actor, then applies
    the computed inserts. An insert at the very end uses ``put`` (which wakes a
    waiting ``get_wait``); anything earlier uses ``put_at``.
    """
    flagged = bool(plan.dj_priority and plan.manager)
    for track in tracks:
        # Always assigned: a re-queued history track must not keep a stale True.
        track.extras.dj_priority = flagged
    if not plan.fair and not flagged:
        queue.put(list(tracks))
        return
    lane = queue.tracks
    size = len(lane)
    for index, track in plan_inserts(lane, tracks, requester_id, flagged=flagged):
        if index >= size:
            queue.put(track)
        else:
            queue.put_at(index, track)
        size += 1
