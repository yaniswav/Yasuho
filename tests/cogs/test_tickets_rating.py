"""V5a: the satisfaction rating flow, the stats math, the CSV and the premium caps.

No real Discord and no real database. The exactly-once / opener-only / window
guarantees are properties of ONE SQL statement, proved against PostgreSQL in
test_tickets_rating_pg.py (skipped without a DSN); here a fake pool answers by the
same rules so the Python flow around them (what is said, what is edited, what is
never called) is covered.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime
import re
import types

import discord
import pytest

from cogs.config.tickets import guild_config, panel, rating, stats, storage
from tools import premium, premium_upsell, settings

OPENER, OTHER, TICKET_ID, GUILD_ID = 7, 8, 42, 5150


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Pool:
    """Answers the rating statements by the same rules as the SQL."""

    def __init__(self, *, opener=OPENER, rating_value=None, expired=False, comment=None):
        self.opener = opener
        self.rating = rating_value
        self.comment = comment
        self.expired = expired
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append(query)
        if query.startswith("UPDATE tickets SET rating = "):
            _tid, value, user, _days = args
            if user != self.opener or self.rating is not None or self.expired:
                return None
            self.rating = value
            return {"guild_id": GUILD_ID, "ticket_number": 3}
        if query.startswith("UPDATE tickets SET rating_comment"):
            _tid, text, user, _days = args
            if user != self.opener or self.rating is None or self.comment is not None:
                return None
            self.comment = text
            return {"guild_id": GUILD_ID, "ticket_number": 3}
        if "FROM tickets WHERE id" in query:
            return {
                "guild_id": GUILD_ID,
                "opener_id": self.opener,
                "status": "closed",
                "rating": self.rating,
                "closed_at": None,
            }
        raise AssertionError(query)


class _Response:
    def __init__(self):
        self.edited = None
        self.sent = None
        self.modal = None
        self.deferred = False

    def is_done(self):
        return self.edited is not None or self.sent is not None or self.deferred

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_message(self, content, **kwargs):
        self.sent = content

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kwargs):
        self.deferred = True


class _Interaction:
    def __init__(self, pool, user_id=OPENER):
        self.client = types.SimpleNamespace(
            db_pool=pool, get_guild=lambda gid: None
        )
        self.user = types.SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = types.SimpleNamespace(sent=[], send=self._followup)
        self.guild_id = GUILD_ID
        self.locale = None

    async def _followup(self, content, **kwargs):
        self.followup.sent.append(content)


# ---------------------------------------------------------------------------
# Persistence: the controls are reconstructed from their custom_id alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
async def test_a_star_button_is_rebuilt_from_its_custom_id_after_a_restart(n):
    original = rating.RatingStarButton(TICKET_ID, n)
    cid = original.item.custom_id
    assert cid == "tkr:{0}:{1}".format(TICKET_ID, n) and len(cid) <= 100
    match = re.fullmatch(rating.RATE_TEMPLATE, cid)
    rebuilt = await rating.RatingStarButton.from_custom_id(None, None, match)
    assert (rebuilt.ticket_id, rebuilt.rating) == (TICKET_ID, n)


async def test_the_comment_button_is_rebuilt_and_the_namespaces_are_disjoint():
    cid = rating.RatingCommentButton(TICKET_ID).item.custom_id
    match = re.fullmatch(rating.COMMENT_TEMPLATE, cid)
    rebuilt = await rating.RatingCommentButton.from_custom_id(None, None, match)
    assert rebuilt.ticket_id == TICKET_ID
    assert not re.fullmatch(rating.RATE_TEMPLATE, cid)
    assert not re.fullmatch(r"tk:(close|claim):\d+", cid)
    assert not re.fullmatch(rating.RATE_TEMPLATE, "tkr:1:6")  # only 1..5


# ---------------------------------------------------------------------------
# The rating click
# ---------------------------------------------------------------------------


async def test_the_opener_rates_once_and_the_prompt_becomes_the_comment_button():
    _on()
    pool = _Pool()
    first = _Interaction(pool)
    await rating._run_rate(first, TICKET_ID, 4)
    assert pool.rating == 4
    assert "tkc:{0}".format(TICKET_ID) in [
        child.item.custom_id for child in first.response.edited["view"].children
    ]

    second = _Interaction(pool)
    await rating._run_rate(second, TICKET_ID, 1)
    assert pool.rating == 4 and second.response.edited is None
    assert "already rated" in second.response.sent


async def test_somebody_else_cannot_rate_the_ticket():
    _on()
    pool = _Pool()
    click = _Interaction(pool, user_id=OTHER)
    await rating._run_rate(click, TICKET_ID, 5)
    assert pool.rating is None and click.response.edited is None
    assert "Only the member who opened" in click.response.sent


async def test_a_late_click_is_told_the_time_has_passed():
    _on()
    pool = _Pool(expired=True)
    click = _Interaction(pool)
    await rating._run_rate(click, TICKET_ID, 5)
    assert pool.rating is None and "time to rate" in click.response.sent


def test_the_window_is_seven_days_and_the_statement_enforces_it():
    assert rating.RATING_WINDOW_DAYS == 7
    assert "closed_at > now() - make_interval(days => $4)" in storage._RATE_TICKET
    assert "rating IS NULL" in storage._RATE_TICKET
    assert "opener_id = $3" in storage._RATE_TICKET


# ---------------------------------------------------------------------------
# The comment
# ---------------------------------------------------------------------------


def test_the_comment_modal_is_bounded_to_300_characters():
    modal = rating.RatingCommentModal(TICKET_ID)
    assert modal.comment.max_length == rating.MAX_COMMENT_LENGTH == 300
    assert modal.comment.required is False  # empty = nothing saved
    assert rating.clean_comment("x" * 1000) == "x" * 300  # also bounded server-side
    assert rating.clean_comment("   ") is None and rating.clean_comment(None) is None


async def test_the_comment_is_saved_once_for_the_opener_only():
    pool = _Pool(rating_value=3)
    mine = _Interaction(pool)
    await rating._run_comment(mine, TICKET_ID, "kind staff")
    assert pool.comment == "kind staff" and "saved" in mine.followup.sent[-1]

    again = _Interaction(pool)
    await rating._run_comment(again, TICKET_ID, "second try")
    assert pool.comment == "kind staff"

    stranger = _Interaction(_Pool(rating_value=3), user_id=OTHER)
    await rating._run_comment(stranger, TICKET_ID, "hi")
    assert "Only the member who opened" in stranger.followup.sent[-1]
    assert "could not be saved" in again.followup.sent[-1]


async def test_an_empty_comment_never_touches_the_database():
    pool = _Pool(rating_value=3)
    click = _Interaction(pool)
    await rating._run_comment(click, TICKET_ID, rating.clean_comment("   "))
    assert pool.calls == [] and click.response.deferred


# ---------------------------------------------------------------------------
# The offer: default OFF, thread first, DM when the thread is archived
# ---------------------------------------------------------------------------


class _User:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, content, **kwargs):
        if self.fail:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "closed")
        self.sent.append((content, kwargs))


class _Bot:
    def __init__(self, cached=None, fetched=None):
        self.db_pool = object()
        self.cached, self.fetched = cached, fetched
        self.fetches = 0

    def get_user(self, uid):
        return self.cached

    async def fetch_user(self, uid):
        self.fetches += 1
        return self.fetched


GUILD = types.SimpleNamespace(id=GUILD_ID, name="Srv")
CLOSED = {"id": TICKET_ID, "opener_id": OPENER, "ticket_number": 3}


def _on():
    settings._cache[("guild_settings", GUILD_ID)] = {guild_config.KEY_RATING: True}


@pytest.fixture(autouse=True)
def _clean_settings():
    settings._cache.clear()
    panel._STATS_COOLDOWN._seen.clear()
    yield
    settings._cache.clear()
    panel._STATS_COOLDOWN._seen.clear()


async def test_the_offer_is_off_by_default():
    user = _User()
    assert await rating.offer_rating(_Bot(user), GUILD, CLOSED) is False
    assert user.sent == []


async def test_the_offer_is_a_dm_with_five_stars_and_the_disclosure():
    _on()
    user = _User()
    assert await rating.offer_rating(_Bot(user), GUILD, CLOSED) is True
    content, kwargs = user.sent[0]
    assert "Staff can see your rating." in content
    assert len(kwargs["view"].children) == 5


async def test_an_uncached_opener_is_fetched_not_looked_up_as_a_member():
    _on()
    user = _User()
    bot = _Bot(cached=None, fetched=user)
    assert await rating.offer_rating(bot, GUILD, CLOSED) is True
    assert bot.fetches == 1 and len(user.sent) == 1


async def test_a_refused_dm_is_logged_at_info_and_reports_false(caplog):
    _on()
    with caplog.at_level("INFO", logger=rating.log.name):
        assert await rating.offer_rating(_Bot(_User(fail=True)), GUILD, CLOSED) is False
    assert any(r.levelname == "INFO" and "not delivered" in r.getMessage() for r in caplog.records)


def test_only_a_real_true_turns_ratings_on():
    assert guild_config.resolve({guild_config.KEY_RATING: True})["rating"] is True
    for junk in (None, False, "true", 1, 0, "1"):
        assert guild_config.resolve({guild_config.KEY_RATING: junk})["rating"] is False


# ---------------------------------------------------------------------------
# Stats math: None is never zero
# ---------------------------------------------------------------------------

T0 = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _row(n, *, claim=None, close=None, by=None, rated=None):
    delta = datetime.timedelta
    return {
        "ticket_number": n,
        "opened_at": T0,
        "claimed_at": None if claim is None else T0 + delta(seconds=claim),
        "closed_at": None if close is None else T0 + delta(seconds=close),
        "claimed_by": by,
        "rating": rated,
    }


def test_an_empty_window_is_all_none_not_zero():
    summary = stats.summarize([])
    assert summary["opened"] == summary["closed"] == 0
    for key in ("claim_median", "claim_mean", "close_median", "close_mean", "rating_mean"):
        assert summary[key] is None
    assert summary["staff"] == [] and summary["distribution"] == {n: 0 for n in range(1, 6)}


def test_tickets_with_no_claim_have_no_claim_time_and_the_median_is_none():
    summary = stats.summarize([_row(1), _row(2, close=100)])
    assert summary["opened"] == 2 and summary["closed"] == 1
    assert summary["claim_median"] is None and summary["close_median"] == 100


def test_median_mean_ratings_and_staff_are_computed_over_real_samples_only():
    rows = [
        _row(1, claim=60, close=600, by=5, rated=5),
        _row(2, claim=120, close=1200, by=5, rated=4),
        _row(3, claim=600, close=300, by=6, rated=None),
        _row(4),  # still open, unclaimed
    ]
    summary = stats.summarize(rows)
    assert summary["claim_median"] == 120 and summary["claim_mean"] == pytest.approx(260)
    assert summary["close_median"] == 600
    assert summary["rating_mean"] == 4.5 and summary["rated"] == 2
    assert summary["distribution"][5] == 1 and summary["distribution"][4] == 1
    assert summary["staff"] == [(5, 2), (6, 1)]


def test_a_pre_migration_claim_without_a_timestamp_adds_no_zero_sample():
    summary = stats.summarize([_row(1, by=5, close=60), _row(2, claim=300, by=5, close=400)])
    assert summary["claim_median"] == 300 and summary["staff"] == [(5, 2)]


def test_the_satisfaction_line_is_a_plural_pair():
    from cogs.config.tickets import stats_render

    one = stats_render.build_embed(stats.summarize([_row(1, rated=5)]), 30, truncated=False)
    two = stats_render.build_embed(
        stats.summarize([_row(1, rated=5), _row(2, rated=4)]), 30, truncated=False
    )
    assert "5.0 out of 5 from 1 rating\n" in one.fields[2].value
    assert "4.5 out of 5 from 2 ratings" in two.fields[2].value


def test_embed_shows_dashes_as_sentences_when_there_is_no_data():
    from cogs.config.tickets import stats_render

    embed = stats_render.build_embed(stats.summarize([_row(1)]), 30, truncated=True)
    values = " ".join(field.value for field in embed.fields)
    assert "No claimed tickets" in values and "No ratings yet" in values
    assert embed.footer.text


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_the_csv_carries_only_the_justified_columns():
    assert stats.HEADER == (
        "ticket", "opened_at", "claimed_at", "closed_at", "claimed_by", "rating"
    )
    text = stats.build_csv([_row(2, claim=5, close=9, by=77, rated=3), _row(1)])
    lines = text.split("\r\n")
    assert lines[0] == ",".join(stats.HEADER)
    assert lines[1].startswith("1,2020-01-01T00:00:00+00:00,,,,")  # oldest first, holes empty
    assert lines[2].endswith(",77,3")
    assert "opener" not in text and "comment" not in text and "closed_by" not in text


def test_the_csv_guard_is_load_bearing(monkeypatch):
    """Negative control: a text cell that looks like a formula is neutralised by
    safe_cell, and the very same input is NOT neutralised once the guard is
    bypassed - so the assertion above really exercises the guard."""
    evil = _row("=HYPERLINK(\"x\")")
    guarded = stats.build_csv([evil])
    assert "'=HYPERLINK" in guarded
    monkeypatch.setattr(stats, "safe_cell", lambda value: "" if value is None else value)
    assert "'=HYPERLINK" not in stats.build_csv([evil])


# ---------------------------------------------------------------------------
# Premium caps and the lapse
# ---------------------------------------------------------------------------


def test_the_limits_are_30_days_free_and_365_with_yasuho_plus():
    assert (premium.GUILD_FREE.ticket_stats_days, premium.GUILD_PREMIUM.ticket_stats_days) == (30, 365)
    assert (premium.GUILD_FREE.ticket_stats_export, premium.GUILD_PREMIUM.ticket_stats_export) == (False, True)
    assert premium.GUILD_CEILINGS["ticket_stats_days"] == 365 == stats.MAX_DAYS


class _Ctx:
    def __init__(self):
        self.guild = types.SimpleNamespace(id=GUILD_ID)
        self.author = types.SimpleNamespace(id=1)
        self.sent = []
        self.deferred = False
        self.deferred_ephemeral = False

    async def defer(self, ephemeral=False):
        self.deferred = True
        self.deferred_ephemeral = ephemeral

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class _StatsPool:
    def __init__(self):
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append(args)
        return [_row(1, claim=60, close=600, by=5, rated=5)]


def _cog(monkeypatch, limits):
    monkeypatch.setattr(premium, "resolve_guild_limits", lambda bot, gid: limits)
    refusals = []

    async def refuse(bot, ctx, text, **kwargs):
        refusals.append((text, kwargs["limit_key"]))

    monkeypatch.setattr(premium_upsell, "refuse_in_context", refuse)
    pool = _StatsPool()
    cog = panel.Tickets(types.SimpleNamespace(db_pool=pool))
    return cog, pool, refusals


async def _run(cog, **kwargs):
    ctx = _Ctx()
    await panel.Tickets.ticket_stats.callback(cog, ctx, **kwargs)
    return ctx


async def test_free_reads_30_days_and_is_refused_beyond_with_the_upsell(monkeypatch):
    cog, pool, refusals = _cog(monkeypatch, premium.GUILD_FREE)
    ctx = await _run(cog, days=30)
    assert ctx.deferred and len(pool.queries) == 1 and refusals == []
    assert ctx.sent[0][1]["embed"].title

    ctx = await _run(cog, days=31)
    assert len(pool.queries) == 1 and not ctx.deferred  # refused before any query
    assert refusals == [("Ticket statistics go back 30 days on the free plan.", "ticket_stats_days")]


async def test_yasuho_plus_reads_up_to_365_days_and_clamps_the_rest(monkeypatch):
    cog, pool, refusals = _cog(monkeypatch, premium.GUILD_PREMIUM)
    await _run(cog, days=365)
    await _run(cog, days=9999)
    assert refusals == []
    cutoffs = [args[1] for args in pool.queries]
    assert all(
        datetime.timedelta(days=364) < datetime.datetime.now(datetime.timezone.utc) - c
        < datetime.timedelta(days=366)
        for c in cutoffs
    )


async def test_the_csv_export_is_yasuho_plus_and_a_lapse_goes_back_to_refusing(monkeypatch):
    cog, pool, refusals = _cog(monkeypatch, premium.GUILD_FREE)
    await _run(cog, days=30, export=True)
    assert refusals == [("The ticket CSV export is a Yasuho+ feature.", "ticket_stats_export")] and not pool.queries

    cog, pool, refusals = _cog(monkeypatch, premium.GUILD_PREMIUM)
    ctx = await _run(cog, days=90, export=True)
    assert refusals == [] and ctx.sent[0][1]["file"].filename == stats.FILENAME

    # Lapse: the same guild, now resolving to FREE, is refused beyond 30 days...
    cog, pool, refusals = _cog(monkeypatch, premium.GUILD_FREE)
    await _run(cog, days=90)
    assert refusals and not pool.queries


async def test_a_click_after_ratings_were_turned_off_is_refused_and_records_nothing():
    pool = _Pool()  # no seed: the server's switch is off
    click = _Interaction(pool)
    await rating._run_rate(click, TICKET_ID, 5)
    assert pool.rating is None and click.response.edited is None
    assert click.response.sent == "Ratings are turned off on this server."


async def test_ratings_keep_working_when_the_subscription_lapses(monkeypatch):
    _on()
    def boom(*args, **kwargs):
        raise AssertionError("the rating flow must never consult premium")

    monkeypatch.setattr(premium, "resolve_guild_limits", boom)
    pool = _Pool()
    click = _Interaction(pool)
    await rating._run_rate(click, TICKET_ID, 5)
    assert pool.rating == 5


def test_the_stats_query_is_bounded_and_never_selects_what_members_typed():
    query = storage._STATS_ROWS
    assert "guild_id = $1 AND opened_at >= $2" in query and "LIMIT $3" in query
    for forbidden in ("opener_id", "rating_comment", "thread_id", "closed_by"):
        assert forbidden not in query


async def test_stats_replies_ephemerally_and_a_refusal_does_not_spend_the_cooldown(monkeypatch):
    cog, pool, refusals = _cog(monkeypatch, premium.GUILD_FREE)
    await _run(cog, days=90)  # refused with the upsell
    assert refusals and panel._STATS_COOLDOWN._seen == {}
    ctx = await _run(cog, days=30)  # the very next valid call still works
    assert ctx.deferred_ephemeral and ctx.sent[0][1]["ephemeral"] is True
    again = await _run(cog, days=30)  # now the 30 s window is spent
    assert "wait a moment" in again.sent[0][0] and len(pool.queries) == 1


async def test_the_staff_line_is_built_inside_the_guild_locale(monkeypatch):
    from cogs.config.tickets import lifecycle
    from tools import i18n

    class _Channel:
        sent = []

        async def send(self, text, **kwargs):
            self.sent.append(text)

    channel = _Channel()

    async def log_channel(guild, pool):
        return channel

    async def guild_locale(bot, guild):
        return "fr"

    monkeypatch.setattr(lifecycle, "_log_channel", log_channel)
    monkeypatch.setattr(rating, "everyone_can_read", lambda ch, unknown: False)
    monkeypatch.setattr(i18n, "resolve_guild_locale", guild_locale)
    seen = []
    bot = types.SimpleNamespace(get_guild=lambda gid: GUILD, db_pool=None)
    await rating._tell_staff(bot, GUILD_ID, lambda: seen.append(i18n.current_locale.get()) or "x")
    assert seen == ["fr"] and channel.sent == ["x"]
