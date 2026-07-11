"""Unit tests for ``cogs/music/voteskip.py`` (democratic skip votes, lot P6).

Everything deterministic without a backend is covered here:

* the pure decision maths - ``count_humans`` (bot exclusion), ``required_votes``
  (2 / 3 / 4 / 8 humans), and ``skip_mode`` (exempt short-circuits regardless of
  room size; a room of <=2 humans skips instantly; a bigger room votes);
* the per-vote bookkeeping on ``SkipVote.record`` - the initiator is seeded as
  the first vote, one vote per member (a re-vote is ``VOTE_ALREADY``), the live
  threshold recomputed against the CURRENT humans (a shrinking room lowers the
  bar), a pass at threshold, a self-cancel when the voted-on track has changed,
  and the closed-vote guard;
* the bounded registry - ``get`` / ``_detach`` (idempotent) / replace and the
  lazy ``_sweep`` that drops resolved votes past the size cap;
* the exemption reuse - the vote path's "who skips instantly" is the P4 effects
  predicate (``effects.is_effect_exempt``: the DJ or a Manage-Server member), NOT
  a duplicated rule, and ``skip_mode`` honours it;
* the surface strings - ``skip_ack`` mapping and the button label format.

Live-only (needs a running loop, a real Discord message and a connected node,
so exercised on the server, not here): posting the public vote message
(``SkipVote.start``), the async follow-through that edits it in place
(``apply`` / ``_update_count`` / ``_resolve`` / ``cancel`` / ``expire``), the view's
30 s timeout, the on-track-start proactive cancel and the ``_clear`` teardown
hook, and the routing seams on the cog (``_request_skip`` / ``_execute_skip``).
``voteskip.py`` imports no sonolink and builds no discord object until ``start``,
so it imports identically under the stub and the real sonolink.
"""

import types

from cogs.music import effects, voteskip

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _member(member_id, *, bot=False):
    return types.SimpleNamespace(id=member_id, bot=bot)


def _track(identifier="T1", title="Song"):
    return types.SimpleNamespace(identifier=identifier, title=title)


def _player(*, humans=0, bots=0, current=None):
    members = [_member(i + 1) for i in range(humans)]
    members += [_member(100 + i, bot=True) for i in range(bots)]
    channel = types.SimpleNamespace(members=members, guild=types.SimpleNamespace(id=7))
    return types.SimpleNamespace(
        current=current, channel=channel, guild=types.SimpleNamespace(id=7), dj=None
    )


def _vote(player, *, initiator_id=1, track=None):
    """Build a SkipVote with a real registry but no posted message (start not called)."""
    initiator = types.SimpleNamespace(id=initiator_id, mention=f"<@{initiator_id}>")
    return voteskip.SkipVote(
        cog=types.SimpleNamespace(),
        player=player,
        channel=types.SimpleNamespace(),
        track=track if track is not None else player.current,
        initiator=initiator,
        registry=voteskip.SkipVotes(),
        guild_id=7,
    )


# ---------------------------------------------------------------------------
# count_humans - bot exclusion
# ---------------------------------------------------------------------------


def test_count_humans_excludes_bots():
    members = [_member(1), _member(2), _member(3, bot=True)]
    assert voteskip.count_humans(members) == 2


def test_count_humans_empty_room():
    assert voteskip.count_humans([]) == 0


def test_count_humans_all_bots():
    assert voteskip.count_humans([_member(1, bot=True), _member(2, bot=True)]) == 0


# ---------------------------------------------------------------------------
# required_votes - ceil(humans / 2) across 2 / 3 / 4 / 8
# ---------------------------------------------------------------------------


def test_required_votes_ceils_half():
    assert voteskip.required_votes(2) == 1
    assert voteskip.required_votes(3) == 2
    assert voteskip.required_votes(4) == 2
    assert voteskip.required_votes(8) == 4


def test_required_votes_small_rooms():
    # A shrunk room of 1 (or 0) still needs a whole vote (never negative / zero
    # for a single human), so the live recount resolves rather than deadlocks.
    assert voteskip.required_votes(0) == 0
    assert voteskip.required_votes(1) == 1


# ---------------------------------------------------------------------------
# skip_mode - exempt short-circuit and the instant-room floor
# ---------------------------------------------------------------------------


def test_skip_mode_exempt_always_instant():
    # A privileged actor skips instantly no matter how full the room is.
    assert voteskip.skip_mode(8, exempt=True) == voteskip.SKIP_INSTANT
    assert voteskip.skip_mode(2, exempt=True) == voteskip.SKIP_INSTANT


def test_skip_mode_tiny_room_instant():
    # <= 2 humans: a 1-of-1 vote is theatre, so skip instantly.
    assert voteskip.skip_mode(1, exempt=False) == voteskip.SKIP_INSTANT
    assert voteskip.skip_mode(2, exempt=False) == voteskip.SKIP_INSTANT


def test_skip_mode_bigger_room_votes():
    assert voteskip.skip_mode(3, exempt=False) == voteskip.SKIP_VOTE
    assert voteskip.skip_mode(4, exempt=False) == voteskip.SKIP_VOTE
    assert voteskip.skip_mode(8, exempt=False) == voteskip.SKIP_VOTE


# ---------------------------------------------------------------------------
# SkipVote.record - the initiator counts, one vote per member
# ---------------------------------------------------------------------------


def test_initiator_is_seeded_as_first_vote():
    player = _player(humans=3, current=_track())
    vote = _vote(player, initiator_id=1)
    assert vote.count() == 1
    assert vote.votes == {1}


def test_record_second_vote_reaches_threshold():
    # 3 humans -> needs 2; the initiator is 1, a second distinct member passes it.
    player = _player(humans=3, current=_track())
    vote = _vote(player, initiator_id=1)
    assert vote.record(2) == voteskip.VOTE_PASSED
    assert vote.count() == 2


def test_record_is_one_vote_per_member():
    player = _player(humans=4, current=_track())  # needs 2
    vote = _vote(player, initiator_id=1)
    # The initiator clicking again does not advance the tally.
    assert vote.record(1) == voteskip.VOTE_ALREADY
    assert vote.count() == 1
    # A distinct member does, and reaches the threshold.
    assert vote.record(2) == voteskip.VOTE_PASSED
    assert vote.count() == 2


def test_record_counts_below_threshold_for_eight():
    # 8 humans -> needs 4: seeded 1, then two more only COUNT, the fourth PASSES.
    player = _player(humans=8, current=_track())
    vote = _vote(player, initiator_id=1)
    assert vote.record(2) == voteskip.VOTE_COUNTED
    assert vote.record(3) == voteskip.VOTE_COUNTED
    assert vote.record(4) == voteskip.VOTE_PASSED
    assert vote.count() == 4


def test_record_self_cancels_when_track_changed():
    player = _player(humans=5, current=_track("T1"))
    vote = _vote(player, track=_track("T1"))
    # The room moved on to a different track: the vote is stale.
    player.current = _track("T2")
    assert vote.record(2) == voteskip.VOTE_ENDED


def test_record_on_resolved_vote_is_noop():
    player = _player(humans=5, current=_track())
    vote = _vote(player)
    vote._resolved = True
    assert vote.record(2) == voteskip.VOTE_ALREADY


def test_shrinking_room_lowers_the_bar():
    # A vote opened in a room of 4 needs 2; if the room shrinks to 2 humans the
    # live recount drops the requirement to 1 (voters who left keep their vote).
    player = _player(humans=4, current=_track())
    vote = _vote(player)
    assert vote.required() == 2
    player.channel.members = [_member(1), _member(2)]
    assert vote.required() == 1


def test_matches_tracks_identity():
    player = _player(humans=3, current=_track("T1"))
    vote = _vote(player, track=_track("T1"))
    assert vote.matches(_track("T1")) is True
    assert vote.matches(_track("T2")) is False
    assert vote.matches(None) is False


# ---------------------------------------------------------------------------
# SkipVotes registry - get / detach / replace / bounded sweep
# ---------------------------------------------------------------------------


def _fake_entry(resolved=False):
    """A stand-in vote for the registry map (only ``resolved`` is read by sweep)."""
    return types.SimpleNamespace(resolved=resolved)


def test_registry_put_and_get():
    reg = voteskip.SkipVotes()
    entry = _fake_entry()
    reg._put(1, entry)
    assert reg.get(1) is entry
    assert reg.count() == 1


def test_registry_replace_same_guild():
    reg = voteskip.SkipVotes()
    first, second = _fake_entry(), _fake_entry()
    reg._put(1, first)
    reg._put(1, second)
    # One entry per guild: the second replaces the first.
    assert reg.get(1) is second
    assert reg.count() == 1


def test_registry_detach_is_idempotent():
    reg = voteskip.SkipVotes()
    reg._put(1, _fake_entry())
    reg._detach(1)
    assert reg.get(1) is None
    reg._detach(1)  # again: no KeyError
    assert reg.count() == 0


def test_registry_sweep_drops_resolved_past_cap():
    reg = voteskip.SkipVotes(sweep_at=2)
    reg._put(1, _fake_entry(resolved=True))
    reg._put(2, _fake_entry(resolved=False))
    # The third put crosses sweep_at (len 3 > 2) and sweeps resolved entries.
    reg._put(3, _fake_entry(resolved=False))
    assert reg.get(1) is None  # resolved -> swept
    assert reg.get(2) is not None
    assert reg.get(3) is not None
    assert reg.count() == 2


# ---------------------------------------------------------------------------
# Exemption reuse - the P4 effects predicate, NOT a duplicated rule
# ---------------------------------------------------------------------------


def test_vote_exemption_reuses_effects_predicate():
    # The vote path exempts exactly the DJ and Manage-Server members, and that
    # decision is effects.is_effect_exempt reused verbatim (see Music._skip_exempt).
    assert effects.is_effect_exempt(dj_id=5, actor_id=5, has_manage_guild=False) is True
    assert effects.is_effect_exempt(dj_id=5, actor_id=9, has_manage_guild=True) is True
    assert effects.is_effect_exempt(dj_id=5, actor_id=9, has_manage_guild=False) is False
    assert effects.is_effect_exempt(dj_id=None, actor_id=9, has_manage_guild=False) is False


def test_voteskip_defines_no_own_exemption_helper():
    # Guard against a future copy of the DJ/manager rule landing in this module.
    assert not hasattr(voteskip, "is_effect_exempt")
    assert not hasattr(voteskip, "is_skip_exempt")


# ---------------------------------------------------------------------------
# Surface strings - ack mapping and the button label
# ---------------------------------------------------------------------------


def test_skip_ack_maps_each_outcome():
    assert voteskip.skip_ack(voteskip.VOTE_OPENED) == "Started a vote to skip."
    assert voteskip.skip_ack(voteskip.VOTE_COUNTED) == "Added your vote to skip."
    assert voteskip.skip_ack(voteskip.VOTE_PASSED) == "Skipped by vote."
    assert voteskip.skip_ack(voteskip.VOTE_ENDED) == "This track already ended."
    assert voteskip.skip_ack(voteskip.VOTE_ALREADY) == "You already voted to skip."


def test_vote_label_renders_live_fraction():
    assert voteskip._vote_label(1, 3) == "Vote skip (1/3)"
    assert voteskip._vote_label(2, 2) == "Vote skip (2/2)"
