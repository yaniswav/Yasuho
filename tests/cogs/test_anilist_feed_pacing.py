"""Catch-up pacing for the AniList feed poller (LOT A1).

After the 2026-08-02 AniList outage (~7h) the whole held backlog was delivered
in one burst, every card inside the same second. These tests pin the fix:

* pacing is PER CHANNEL - a channel with an ordinary 1-3 cards is never paced,
  no matter how many other channels the same tick fans out to, and no matter
  whether one of them is catching up,
* a bursting channel spaces its cards at the 1-per-10s target,
* the tick's TOTAL sleeping is capped by the pacing budget whatever the fleet
  does, and it degrades smoothly (the tail goes unpaced) instead of falling off
  a cliff,
* the budget shrinks by what the tick already spent, so pacing can never be the
  thing that pushes a tick past its poll period,
* the pacing sleep is cancellable - ``cog_unload`` during a paced catch-up tears
  the poll loop down at once instead of hanging for a 10s gap,
* the cursor / dedup / coalescing machinery is untouched by pacing.

Everything here is offline: the cog is built with ``__new__`` and fed hand-rolled
fakes; ``asyncio.sleep`` is captured (except in the cancellation test, which uses
the real clock on purpose).
"""

import asyncio
import time

import pytest

from cogs.anilist import feed as feed_mod
from cogs.anilist import feed_policy as af
from cogs.anilist.feed import AniListFeed, _CatchUpPacer

# Bound before any test patches ``asyncio.sleep``, so the fake clock can still
# yield to the event loop while the pacer's own sleep is captured.
_real_sleep = asyncio.sleep

# The most rich cards one channel can receive in a tick, hence the most gaps it
# can ever contribute: plan_posts collapses everything past MAX_FULL into a
# single digest.
MAX_GAPS_PER_CHANNEL = af.MAX_FULL_POSTS_PER_TICK - 1


# --- Fakes ------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class _FakeChannel:
    """Records sends with the monotonic-ish timestamp the fake clock had."""

    def __init__(self, channel_id=100, clock=None):
        self.id = channel_id
        # resolve_guild_locale returns the default locale for a None guild, so
        # delivery needs no locale DB access here.
        self.guild = None
        self.sends = []
        self._clock = clock
        self._next_message_id = 5000

    def is_nsfw(self):
        return False

    async def send(self, **kwargs):
        self._next_message_id += 1
        at = self._clock.now if self._clock is not None else 0.0
        self.sends.append((self._next_message_id, at, kwargs))
        return _FakeMessage(self._next_message_id)

    def get_partial_message(self, message_id):  # pragma: no cover - unused here
        raise AssertionError("text activities never coalesce")


class _FakePool:
    def __init__(self):
        self.executes = []

    async def fetchrow(self, sql, *args):  # pragma: no cover - unused here
        return None

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "OK"


class _FakeBot:
    def __init__(self, pool, channels):
        self.db_pool = pool
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def wait_until_ready(self):
        return None

    def add_dynamic_items(self, *items):
        return None

    def remove_dynamic_items(self, *items):
        return None


class _Clock:
    """Fake clock driven by the patched ``asyncio.sleep``."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    async def sleep(self, delay, *args, **kwargs):
        self.sleeps.append(delay)
        self.now += delay
        # Stay a real suspension point so ordering bugs still surface.
        await _real_sleep(0)


def _cog(channels, pool=None):
    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = _FakeBot(pool or _FakePool(), channels)
    return cog


def _text_activity(activity_id, user_id=7):
    """A TEXT activity: never coalescible, so delivery is one plain send."""

    return {
        "id": activity_id,
        "kind": "TextActivity",
        "type": "TEXT",
        "user_id": user_id,
        "user": {"id": user_id, "name": "reader"},
        "created_at": 1_700_000_000 + activity_id,
        "site_url": "https://anilist.co/activity/%s" % activity_id,
        "is_adult": False,
        "text": "hello",
    }


def _feed_row(channel_id, guild_id=1):
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "types": ["TEXT"],
        "fail_count": 0,
    }


# --- The pacer itself -------------------------------------------------------


@pytest.mark.parametrize("cards", [0, 1, 2, 3])
def test_an_ordinary_channel_is_never_paced(cards):
    # 1-3 cards is what an ordinary 120s tick brings a channel. Even inside a
    # tick that IS pacing (a real gap), such a channel is left alone.
    pacer = _CatchUpPacer(MAX_GAPS_PER_CHANNEL)
    assert pacer.gap  # the tick really is paced...
    assert pacer.paces(cards) is False  # ...this channel still is not


def test_pacing_engages_at_the_catch_up_threshold():
    # The boundary is real: MIN_CARDS - 1 is idle, MIN_CARDS paces.
    pacer = _CatchUpPacer(MAX_GAPS_PER_CHANNEL)
    assert pacer.paces(feed_mod.CATCHUP_MIN_CARDS - 1) is False
    assert pacer.paces(feed_mod.CATCHUP_MIN_CARDS) is True


def test_a_tick_with_no_bursting_channel_has_no_gap():
    # Zero gaps planned -> the pacer is inert and paces nobody, which is the
    # shape of every ordinary tick at ANY fleet size.
    pacer = _CatchUpPacer(0)
    assert pacer.gap == 0.0
    assert pacer.paced_gaps == 0
    assert pacer.paces(af.MAX_FULL_POSTS_PER_TICK) is False


def test_a_lone_bursting_channel_uses_the_full_ten_second_target():
    # Up to the number of gaps that fit at the target, the gap is untouched.
    for gaps in range(1, feed_mod.CATCHUP_TARGET_GAPS + 1):
        assert _CatchUpPacer(gaps).gap == feed_mod.CATCHUP_SPACING


def test_gap_compresses_past_the_target_instead_of_bursting():
    # One gap past the target: the batch no longer fits at 10s, so the gap
    # shrinks - it does not fall back to a burst.
    pacer = _CatchUpPacer(feed_mod.CATCHUP_TARGET_GAPS + 1)
    assert feed_mod.CATCHUP_MIN_SPACING <= pacer.gap < feed_mod.CATCHUP_SPACING


@pytest.mark.parametrize(
    "gaps", [1, 3, 4, 7, 8, 12, 29, 30, 31, 32, 100, 500, 5000]
)
def test_pacing_never_exceeds_the_per_tick_budget(gaps):
    # THE bound: whatever the backlog, a tick cannot sleep past the budget, so
    # it cannot outlive the poll interval and collapse the poll cadence.
    pacer = _CatchUpPacer(gaps)
    assert pacer.gap >= feed_mod.CATCHUP_MIN_SPACING
    assert pacer.paced_gaps <= gaps
    assert pacer.paced_gaps * pacer.gap <= feed_mod.CATCHUP_PACING_BUDGET + 1e-9


def test_a_huge_backlog_paces_its_head_and_sends_the_tail_unpaced():
    # At the affordable maximum every gap is still paced, at the floor...
    at_cap = _CatchUpPacer(feed_mod.CATCHUP_MAX_PACED_GAPS)
    assert at_cap.gap == pytest.approx(feed_mod.CATCHUP_MIN_SPACING)
    assert at_cap.paced_gaps == feed_mod.CATCHUP_MAX_PACED_GAPS
    # ...and one gap past it the tail simply goes unpaced. Pacing does NOT
    # switch off wholesale, so the wall clock does not jump.
    past_cap = _CatchUpPacer(feed_mod.CATCHUP_MAX_PACED_GAPS + 1)
    assert past_cap.gap == pytest.approx(feed_mod.CATCHUP_MIN_SPACING)
    assert past_cap.paced_gaps == feed_mod.CATCHUP_MAX_PACED_GAPS


def test_the_wall_clock_has_no_cliff_in_it():
    """One more card can never swing the tick's sleeping by a whole minute.

    The regression this pins: a design that switches pacing OFF once the gap
    would fall under the floor spends the entire budget at N gaps and nothing at
    all at N + 1, and ``planned_gaps`` is noisy tick to tick.
    """
    totals = [
        _CatchUpPacer(gaps).paced_gaps * _CatchUpPacer(gaps).gap
        for gaps in range(1, 400)
    ]
    for before, after in zip(totals, totals[1:]):
        assert abs(after - before) <= feed_mod.CATCHUP_SPACING + 1e-9
    assert max(totals) <= feed_mod.CATCHUP_PACING_BUDGET + 1e-9


def test_the_documented_handles_match_the_budget_maths():
    # The constants are derived, not hand-typed: a 30s budget / 10s target -> 3
    # gaps at full spacing, 30s / 1s floor -> 30 gaps paced at all.
    assert feed_mod.CATCHUP_PACING_BUDGET == feed_mod.POLL_SECONDS / 4.0
    assert feed_mod.CATCHUP_TARGET_GAPS == 3
    assert feed_mod.CATCHUP_MAX_PACED_GAPS == 30


def test_a_spent_budget_leaves_the_pacer_inert():
    # What _tick hands over when the fetch has already eaten the ceiling.
    pacer = _CatchUpPacer(MAX_GAPS_PER_CHANNEL, budget=0.0)
    assert pacer.gap == 0.0
    assert pacer.paced_gaps == 0
    assert pacer.paces(af.MAX_FULL_POSTS_PER_TICK) is False


def test_a_shrunken_budget_paces_less_not_longer():
    # A slow fetch may only shorten the tick's sleeping, never extend it.
    full = _CatchUpPacer(MAX_GAPS_PER_CHANNEL)
    partial = _CatchUpPacer(MAX_GAPS_PER_CHANNEL, budget=5.0)
    assert partial.paced_gaps * partial.gap <= 5.0
    assert partial.paced_gaps * partial.gap < full.paced_gaps * full.gap


async def test_the_first_card_of_a_channel_is_never_delayed(monkeypatch):
    # The gap sits BETWEEN cards: a bursting channel still starts delivering
    # immediately, it just does not dump the rest behind it.
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel})
    items = [_text_activity(i) for i in range(1, af.MAX_FULL_POSTS_PER_TICK + 1)]

    await cog._dispatch([_feed_row(100)], {(1, 100): {7}}, items)

    assert channel.sends[0][1] == 0.0
    assert len(clock.sleeps) == MAX_GAPS_PER_CHANNEL


# --- Delivery wiring --------------------------------------------------------


async def test_two_card_tick_delivers_back_to_back(monkeypatch):
    # Mutation-real: any sleep on the normal path fails here.
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel})
    items = [_text_activity(1), _text_activity(2)]

    await cog._dispatch(
        [_feed_row(100)], {(1, 100): {7}}, items
    )

    assert len(channel.sends) == 2
    assert clock.sleeps == []
    assert [s[1] for s in channel.sends] == [0.0, 0.0]  # same instant, as before


async def test_catch_up_channel_spaces_its_cards(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel})
    # MAX_FULL_POSTS_PER_TICK cards, all full (no digest).
    items = [_text_activity(i) for i in range(1, af.MAX_FULL_POSTS_PER_TICK + 1)]

    await cog._dispatch([_feed_row(100)], {(1, 100): {7}}, items)

    assert len(channel.sends) == af.MAX_FULL_POSTS_PER_TICK
    # 4 gaps do not fit at the 10s target inside a 30s budget, so they compress
    # to 7.5s each and the channel's cards land 7.5s apart.
    gap = feed_mod.CATCHUP_PACING_BUDGET / MAX_GAPS_PER_CHANNEL
    assert clock.sleeps == [gap] * MAX_GAPS_PER_CHANNEL
    assert [s[1] for s in channel.sends] == [0.0, 7.5, 15.0, 22.5, 30.0]


async def test_fan_out_across_forty_guilds_is_not_paced(monkeypatch):
    """THE finding this lot's first cut got wrong.

    Forty guilds, ONE new activity, one card each: nobody is catching up, so a
    tick-wide threshold would have injected 39 gaps and stretched a routine tick
    to the full budget for ever. Nobody reads two guilds' feeds at once, so the
    burst a reader can see is one card - and there is nothing to pace.
    """
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel_ids = list(range(100, 140))
    channels = {cid: _FakeChannel(cid, clock) for cid in channel_ids}
    cog = _cog(channels)

    feeds = [_feed_row(cid, guild_id=cid) for cid in channel_ids]
    follows = {(cid, cid): {7} for cid in channel_ids}

    await cog._dispatch(feeds, follows, [_text_activity(1)])

    assert sum(len(c.sends) for c in channels.values()) == 40
    assert clock.sleeps == []
    assert clock.now == 0.0


async def test_an_ordinary_channel_is_not_paced_beside_a_bursting_one(monkeypatch):
    """The gap never straddles a channel boundary, and never touches a quiet one.

    Channel 100 is catching up (5 cards); channel 101 has the single new
    activity everyone got. 101's card must go out at full speed, and it must not
    wait on 100's last card either.
    """
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channels = {100: _FakeChannel(100, clock), 101: _FakeChannel(101, clock)}
    cog = _cog(channels)
    # User 7 is followed by both channels, user 8 only by channel 100.
    items = [_text_activity(1, user_id=7)] + [
        _text_activity(i, user_id=8) for i in range(2, 6)
    ]
    feeds = [_feed_row(100), _feed_row(101, guild_id=2)]
    follows = {(1, 100): {7, 8}, (2, 101): {7}}

    await cog._dispatch(feeds, follows, items)

    assert len(channels[100].sends) == af.MAX_FULL_POSTS_PER_TICK
    assert len(channels[101].sends) == 1
    # Only the bursting channel's own gaps were slept.
    assert len(clock.sleeps) == MAX_GAPS_PER_CHANNEL
    # The quiet channel's single card rode straight behind the paced one, with
    # no gap of its own added after it.
    assert channels[101].sends[0][1] == channels[100].sends[-1][1]


async def test_many_bursting_channels_share_one_budget(monkeypatch):
    # Ten channels each catching up with a full plan_posts page: 10 x 4 gaps.
    # The gap compresses so the WHOLE tick still fits the budget.
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel_ids = list(range(100, 110))
    channels = {cid: _FakeChannel(cid, clock) for cid in channel_ids}
    cog = _cog(channels)

    items = [_text_activity(i) for i in range(1, 31)]
    feeds = [_feed_row(cid, guild_id=cid) for cid in channel_ids]
    follows = {(cid, cid): {7} for cid in channel_ids}

    await cog._dispatch(feeds, follows, items)

    sent = sum(len(c.sends) for c in channels.values())
    cards = 10 * af.MAX_FULL_POSTS_PER_TICK  # 50 cards...
    digests = 10  # ...plus one digest per channel, never paced
    assert sent == cards + digests

    # 40 gaps wanted; the budget affords 30 at the floor, and the rest of the
    # cards go out back to back rather than the whole tick giving up on pacing.
    assert len(clock.sleeps) == feed_mod.CATCHUP_MAX_PACED_GAPS
    assert clock.sleeps == [feed_mod.CATCHUP_MIN_SPACING] * (
        feed_mod.CATCHUP_MAX_PACED_GAPS
    )
    assert clock.now == pytest.approx(feed_mod.CATCHUP_PACING_BUDGET)
    # Bounded well inside the poll period, which is the point of the budget.
    assert clock.now < feed_mod.POLL_SECONDS


async def test_a_spent_budget_delivers_at_full_speed(monkeypatch):
    # What a slow fetch hands to _dispatch: no budget left, so a catch-up
    # channel is delivered unpaced rather than pushing the tick past its period.
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel})
    items = [_text_activity(i) for i in range(1, af.MAX_FULL_POSTS_PER_TICK + 1)]

    await cog._dispatch(
        [_feed_row(100)], {(1, 100): {7}}, items, pacing_budget=0.0
    )

    assert len(channel.sends) == af.MAX_FULL_POSTS_PER_TICK
    assert clock.sleeps == []


async def test_digest_is_not_paced(monkeypatch):
    # A channel over the full-card cap gets 5 cards + 1 digest; only the cards
    # are spaced, so the digest rides straight behind the last card.
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel})
    items = [_text_activity(i) for i in range(1, 9)]  # 5 full + 3 digested

    await cog._dispatch([_feed_row(100)], {(1, 100): {7}}, items)

    assert len(channel.sends) == af.MAX_FULL_POSTS_PER_TICK + 1
    assert len(clock.sleeps) == MAX_GAPS_PER_CHANNEL
    # Last card and digest share the same instant.
    assert channel.sends[-1][1] == channel.sends[-2][1]


async def test_delivery_without_a_pacer_is_unchanged(monkeypatch):
    # Every non-poller caller of _deliver_channel keeps full-speed delivery.
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel})
    items = [_text_activity(i) for i in range(1, 6)]

    await cog._deliver_channel(_feed_row(100), 100, items)

    assert len(channel.sends) == 5
    assert clock.sleeps == []


async def test_pacing_writes_nothing_and_touches_no_cursor(monkeypatch):
    # Pacing is presentation only: it must not add a DB write, and _dispatch has
    # no business near the cursor (which _tick advances after it returns).
    clock = _Clock()
    monkeypatch.setattr(feed_mod.asyncio, "sleep", clock.sleep)

    pool = _FakePool()
    channel = _FakeChannel(100, clock)
    cog = _cog({100: channel}, pool=pool)
    items = [_text_activity(i) for i in range(1, 6)]

    await cog._dispatch([_feed_row(100)], {(1, 100): {7}}, items)

    # Text activities never coalesce, so a paced tick writes exactly nothing.
    assert pool.executes == []
    assert not hasattr(cog, "_last_paced_cursor")  # no shadow cursor invented


# --- The tick charges its own fetch against the budget ----------------------


def _tick_harness(cog, monkeypatch, elapsed):
    """Stub _tick's I/O and freeze its clock so only the budget maths is live.

    ``elapsed`` is the wall clock the fetch is made to consume. Everything else
    (the prune, the three loads, the save) is a no-op.
    """
    captured = {}

    async def _noop(*args, **kwargs):
        return None

    async def _load_feeds():
        return [_feed_row(100)]

    async def _load_follows():
        return [{"guild_id": 1, "channel_id": 100, "anilist_user_id": 7}]

    async def _load_state():
        return 0, 1_000

    async def _fetch(user_ids, last_created):
        clock["now"] += elapsed
        return [_text_activity(i) for i in range(1, 6)], None

    async def _dispatch(feeds, follows, activities, *, pacing_budget):
        captured["budget"] = pacing_budget

    clock = {"now": 0.0}
    monkeypatch.setattr(feed_mod, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(feed_mod, "_normalize", lambda raw: raw)
    cog._embargo_until = 0
    cog._prune_coalesce_posts = _noop
    cog._load_feeds = _load_feeds
    cog._load_follows = _load_follows
    cog._load_state = _load_state
    cog._fetch_activities = _fetch
    cog._save_state = _noop
    cog._dispatch = _dispatch
    return captured


async def test_a_quick_tick_hands_the_whole_budget_to_the_pacer(monkeypatch):
    cog = _cog({100: _FakeChannel(100)})
    captured = _tick_harness(cog, monkeypatch, elapsed=0.0)

    await cog._tick()

    assert captured["budget"] == feed_mod.CATCHUP_PACING_BUDGET


async def test_a_slow_fetch_is_charged_against_the_pacing_budget(monkeypatch):
    # The fetch spaces its requests by REQUEST_SPACING and grows with the
    # followed union, so it is the part of the tick that can actually get long.
    cog = _cog({100: _FakeChannel(100)})
    captured = _tick_harness(cog, monkeypatch, elapsed=20.0)

    await cog._tick()

    assert captured["budget"] == pytest.approx(
        feed_mod.CATCHUP_PACING_BUDGET - 20.0
    )


async def test_a_fetch_that_ate_the_ceiling_leaves_no_budget_at_all(monkeypatch):
    """THE bound: pacing can never be the thing that pushes a tick past its poll
    period, because a tick that already spent the ceiling gets a budget of 0 and
    an inert pacer - it does not go negative and it does not sleep."""
    cog = _cog({100: _FakeChannel(100)})
    captured = _tick_harness(
        cog, monkeypatch, elapsed=feed_mod.POLL_SECONDS * 2
    )

    await cog._tick()

    assert captured["budget"] == 0.0
    assert _CatchUpPacer(MAX_GAPS_PER_CHANNEL, budget=0.0).gap == 0.0


# --- Cancellation (real clock) ----------------------------------------------


async def test_cog_unload_cancels_a_paced_catch_up_promptly(monkeypatch):
    """A ``cog_unload`` mid-gap must tear the poll loop down, not wait the gap out.

    Runs the REAL ``tasks.loop`` on the REAL clock, through the real
    ``cog_unload`` -> ``self._poll_feeds.cancel()`` path, and parks the poller in
    a real pacing sleep. If that sleep were not cancellable - shielded, or with
    anything on the delivery path catching ``asyncio.CancelledError`` (a
    ``BaseException``, so ``except Exception`` correctly misses it) - the loop
    task would still be pending below and the assertion would fail.

    The gap is shortened for this test only: what is under test is that the gap
    IS a cancellable sleep inside the poll task, not how long it is (the other
    tests pin the real 10s). A short gap also keeps a future regression's
    failure quick instead of parking the suite in a 10s sleep.
    """

    monkeypatch.setattr(feed_mod, "CATCHUP_MIN_SPACING", 0.05)
    monkeypatch.setattr(feed_mod, "CATCHUP_SPACING", 0.5)

    channel = _FakeChannel(100)
    cog = _cog({100: channel})
    items = [_text_activity(i) for i in range(1, 6)]

    async def _tick():
        await cog._dispatch([_feed_row(100)], {(1, 100): {7}}, items)

    cog._tick = _tick
    cog._poll_feeds.start()
    task = cog._poll_feeds.get_task()
    try:
        # Wait until the first card is out and the poller is parked in a gap.
        started = time.monotonic()
        while not channel.sends:
            await _real_sleep(0)
            assert time.monotonic() - started < 5.0, "delivery never started"
        assert not task.done()  # genuinely mid-tick, not already finished

        cog.cog_unload()

        deadline = time.monotonic() + 0.2
        while not task.done() and time.monotonic() < deadline:
            await _real_sleep(0.005)
        assert task.done(), "cog_unload did not interrupt the pacing sleep"
        assert task.cancelled()
    finally:
        # Bounded hard stop so a regression cannot leave a live poll task behind.
        for _ in range(60):
            if task.done():
                break
            task.cancel()
            await _real_sleep(0.05)

    # It stopped INSIDE the gap: the rest of the backlog was never sent.
    assert len(channel.sends) == 1
    assert time.monotonic() - started < feed_mod.CATCHUP_SPACING
