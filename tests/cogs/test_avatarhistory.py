import io
import types

import discord
import pytest
from PIL import Image

from cogs.community import avatarhistory
from cogs.community.usersettings import PREFS
from tools.cooldowns import Cooldowns


@pytest.fixture(autouse=True)
def _fresh_render_debounce(monkeypatch):
    """The button window is module state (bounded, process-wide). Each test gets
    its own, or the first click would throttle every test after it.

    Built from the real object's OWN window, never from the constant, so the
    test that asserts the two doors share one window is not asserting against a
    value this fixture just made up.
    """
    monkeypatch.setattr(
        avatarhistory,
        "_RENDER_DEBOUNCE",
        Cooldowns(avatarhistory._RENDER_DEBOUNCE.seconds),
    )


def _png(size=(512, 512)):
    image = Image.new("RGBA", size, (120, 40, 200, 180))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_storage_compression_outputs_bounded_webp():
    compressed = avatarhistory.AvatarHistory.compress_for_storage(_png())

    with Image.open(io.BytesIO(compressed)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= avatarhistory.STORAGE_MAX_SIZE


async def test_record_respects_tracking_opt_out(monkeypatch):
    calls = []

    async def _get_user(pool, user_id, key, default):
        calls.append((user_id, key, default))
        return False

    monkeypatch.setattr(avatarhistory.settings, "get_user", _get_user)
    cog = object.__new__(avatarhistory.AvatarHistory)
    cog.bot = types.SimpleNamespace(db_pool=object())

    class _Asset:
        @property
        def key(self):
            raise AssertionError("asset must not be touched after opt-out")

    await cog._record(42, None, "global", _Asset())

    assert calls == [(42, avatarhistory.TRACKING_PREF_KEY, True)]


async def test_capture_banner_skips_fetch_user_when_opted_out(monkeypatch):
    """The opt-out check is a warm cached read; it must run BEFORE the
    uncached ``fetch_user`` REST call, so an opted-out user costs zero
    network round-trips."""
    calls = []

    async def _get_user(pool, user_id, key, default):
        calls.append((user_id, key, default))
        return False

    async def _fetch_user(user_id):
        raise AssertionError("fetch_user must not be called when opted out")

    monkeypatch.setattr(avatarhistory.settings, "get_user", _get_user)
    cog = object.__new__(avatarhistory.AvatarHistory)
    cog.bot = types.SimpleNamespace(db_pool=object(), fetch_user=_fetch_user)

    await cog.capture_banner(types.SimpleNamespace(id=99))

    assert calls == [(99, avatarhistory.TRACKING_PREF_KEY, True)]


async def test_capture_banner_fetches_user_when_opted_in(monkeypatch):
    async def _get_user(pool, user_id, key, default):
        return True

    fetched_ids = []
    fake_user = types.SimpleNamespace(banner=None)

    async def _fetch_user(user_id):
        fetched_ids.append(user_id)
        return fake_user

    monkeypatch.setattr(avatarhistory.settings, "get_user", _get_user)
    cog = object.__new__(avatarhistory.AvatarHistory)
    cog.bot = types.SimpleNamespace(db_pool=object(), fetch_user=_fetch_user)

    await cog.capture_banner(types.SimpleNamespace(id=99))

    assert fetched_ids == [99]


async def test_capture_banner_reuses_a_user_the_caller_already_fetched(monkeypatch):
    """?userinfo fetches the user itself to render the banner it is about to
    show, then archives it - so without the handover this cog fetched the very
    same uncached user a SECOND time, two REST calls per invocation for one
    user's banner. The recorded asset must come from the handed-over object."""
    async def _get_user(pool, user_id, key, default):
        return True

    async def _fetch_user(user_id):
        raise AssertionError("capture_banner must not re-fetch a supplied user")

    recorded = []

    async def _record(user_id, guild_id, kind, asset):
        recorded.append((user_id, guild_id, kind, asset))

    monkeypatch.setattr(avatarhistory.settings, "get_user", _get_user)
    cog = object.__new__(avatarhistory.AvatarHistory)
    cog.bot = types.SimpleNamespace(db_pool=object(), fetch_user=_fetch_user)
    cog._record = _record
    already = types.SimpleNamespace(banner=types.SimpleNamespace(key="bnr"))

    await cog.capture_banner(types.SimpleNamespace(id=99), fetched=already)

    assert recorded == [(99, None, "banner", already.banner)]


async def test_userinfo_fetches_the_user_once_and_hands_it_to_the_archiver(
    monkeypatch,
):
    """The end-to-end count, at the seam that pays it: ONE fetch_user per
    ?userinfo, whose result feeds both the card and the archive.

    The body moved under the /info group (``Info.info_user``) when the command
    tree was folded to stay under Discord's 100-command cap; ``?userinfo`` now
    reaches it through a prefix-only shim. This still drives the one body both
    surfaces share.
    """
    from cogs.utility import info as info_module

    fetched_ids = []
    full = types.SimpleNamespace(banner=None)

    async def _fetch_user(user_id):
        fetched_ids.append(user_id)
        return full

    captured = []

    class _FakeAvatarHistory:
        async def capture_banner(self, user, *, fetched=None):
            captured.append((user.id, fetched))

    sent = []

    class _Ctx:
        async def send(self, **kwargs):
            sent.append(kwargs)

    history = _FakeAvatarHistory()
    cog = object.__new__(info_module.Info)
    cog.bot = types.SimpleNamespace(
        fetch_user=_fetch_user, get_cog=lambda name: history
    )
    member = types.SimpleNamespace(id=99)
    monkeypatch.setattr(
        info_module, "UserInfoView", lambda member, banner_url=None: object()
    )

    await info_module.Info.info_user.callback(cog, _Ctx(), member)

    assert fetched_ids == [99]  # exactly one REST call, not two
    assert captured == [(99, full)]
    assert sent  # the card still went out


def test_avatar_tracking_is_available_in_user_preferences():
    pref = next(
        item for item in PREFS if item.key == avatarhistory.TRACKING_PREF_KEY
    )
    assert pref.default is True


def test_avatar_series_limit_matches_approved_retention_policy():
    assert avatarhistory.HISTORY_LIMIT == 30


# ---------------------------------------------------------------------------
# WAVE-B-B1: a member joining must cost ZERO REST calls
# ---------------------------------------------------------------------------


def _listener_names():
    return {name for name, _ in avatarhistory.AvatarHistory.__cog_listeners__}


def test_a_join_costs_no_rest_call():
    """THE SCALE BUG: on_member_join used to fetch_user() every joining member,
    bots included, with no config gate and no cooldown - one uncached REST call
    per join across every guild the bot is in, to pre-warm a banner history
    nobody may ever open. The hook is gone; the Banner button captures the same
    banner lazily, at the moment somebody actually asks for it.

    The second half also holds the line if a join hook ever comes back: it may
    not reach the network on its own.
    """
    assert "on_member_join" not in _listener_names()

    hook = getattr(avatarhistory.AvatarHistory, "on_member_join", None)
    assert hook is None, "a join hook is back - it must not fetch (see below)"


async def test_a_bot_joining_is_never_fetched():
    """A bot account never opted into anything here and has no banner history
    worth keeping; it must cost nothing at all."""
    async def _fetch_user(user_id):
        raise AssertionError("a join must not reach the REST API")

    cog = object.__new__(avatarhistory.AvatarHistory)
    cog.bot = types.SimpleNamespace(db_pool=object(), fetch_user=_fetch_user)

    hook = getattr(cog, "on_member_join", None)
    if hook is not None:  # pragma: no cover - only if the hook ever returns
        await hook(types.SimpleNamespace(id=1, bot=True))
        await hook(types.SimpleNamespace(id=2, bot=False))
    assert "on_member_join" not in _listener_names()


def test_the_pushed_avatar_listeners_are_untouched():
    """Dropping the join hook must not cost the AVATAR capture: Discord pushes
    those over the gateway, so they stay free (no REST call at all)."""
    assert {"on_user_update", "on_member_update"} <= _listener_names()


async def test_the_banner_button_still_captures_lazily(monkeypatch):
    """The lazy capture that now covers for the dropped join hook: pressing
    Banner captures the banner then and there."""
    captured = []

    cog = object.__new__(avatarhistory.AvatarHistory)

    async def _capture(member):
        captured.append(member.id)

    async def _build_payload(member, kind, guild_id):
        return "embed", None

    cog.capture_banner = _capture
    cog.build_payload = _build_payload

    member = types.SimpleNamespace(id=77)
    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=5), guild=None)
    view = avatarhistory.AvatarHistoryView(cog, ctx, member)

    edits = []

    class _Message:
        async def edit(self, **kwargs):
            edits.append(kwargs)

    class _Response:
        async def defer(self):
            pass

    view.message = _Message()
    await view._show(_interaction(), "banner")

    assert captured == [77]
    assert len(edits) == 1


async def test_the_global_history_view_never_captures_a_banner():
    """Only the Banner button pays for a fetch - opening a history does not."""
    cog = object.__new__(avatarhistory.AvatarHistory)
    captured = []
    rendered = []

    async def _capture(member):
        captured.append(member.id)

    async def _build_payload(member, kind, guild_id):
        rendered.append(kind)
        return "embed", None

    cog.capture_banner = _capture
    cog.build_payload = _build_payload

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=5), guild=None)
    view = avatarhistory.AvatarHistoryView(cog, ctx, types.SimpleNamespace(id=77))

    class _Message:
        async def edit(self, **kwargs):
            pass

    class _Response:
        async def defer(self):
            pass

    view.message = _Message()
    await view._show(_interaction(), "global")

    assert captured == []
    assert rendered == ["global"]  # the render really ran, so this is not vacuous


# ---------------------------------------------------------------------------
# The audience: whose history you may pull, and how often you may repaint one
# ---------------------------------------------------------------------------


def _interaction(user_id=5):
    """A component interaction stand-in: who clicked, plus the two response
    calls _show can make (defer on the way in, ephemeral refusal on a throttle).
    """
    sent = []

    class _Response:
        async def defer(self):
            sent.append(("defer", {}))

        async def send_message(self, content, **kwargs):
            sent.append((content, kwargs))

    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=user_id), response=_Response(), sent=sent
    )


class _Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


class _CmdCtx:
    """A commands.Context stand-in for ``?avatarhistory`` / ``/avatarhistory``."""

    def __init__(self, author_id=5, guild=None):
        self.author = types.SimpleNamespace(id=author_id)
        self.guild = guild
        self.sends = []

    def typing(self):
        return _Typing()

    async def send(self, content=None, **kwargs):
        self.sends.append((content, kwargs))
        return types.SimpleNamespace(id=1)


class _Guild:
    def __init__(self, guild_id=1, members=(), fetch=None):
        self.id = guild_id
        self._members = {uid: types.SimpleNamespace(id=uid) for uid in members}
        self._fetch = fetch
        self.fetched = []

    def get_member(self, user_id):
        return self._members.get(user_id)

    async def fetch_member(self, user_id):
        self.fetched.append(user_id)
        if self._fetch is None:
            raise AssertionError("this test expected no REST confirmation")
        return await self._fetch(user_id)


def _http_error(status, cls=discord.HTTPException):
    return cls(types.SimpleNamespace(status=status, reason="x"), "x")


def _command_cog():
    """A cog whose only live part is the render, recorded rather than run."""
    cog = object.__new__(avatarhistory.AvatarHistory)
    rendered = []

    async def _build_payload(member, kind, guild_id):
        rendered.append((member.id, kind, guild_id))
        return "embed", None

    cog.build_payload = _build_payload
    cog.rendered = rendered
    return cog


async def test_your_own_history_needs_no_server_at_all():
    cog = _command_cog()
    ctx = _CmdCtx(author_id=5, guild=None)  # a DM

    await avatarhistory.AvatarHistory.avatarhistory.callback(cog, ctx, None)

    assert cog.rendered == [(5, "global", None)]


async def test_someone_elses_history_is_refused_outside_a_shared_server():
    """THE LEAK: the command took a raw user id and answered for ANY account on
    Discord, so anyone could pull the stored faces of a person they share no
    server with - someone with no way of even knowing this bot exists."""
    cog = _command_cog()
    ctx = _CmdCtx(author_id=5, guild=None)
    stranger = types.SimpleNamespace(id=4242)

    await avatarhistory.AvatarHistory.avatarhistory.callback(cog, ctx, stranger)

    assert cog.rendered == []
    assert ctx.sends and ctx.sends[0][1].get("ephemeral") is True


async def test_a_stranger_the_api_confirms_is_not_here_is_refused():
    async def _fetch(user_id):
        raise _http_error(404, discord.NotFound)

    cog = _command_cog()
    ctx = _CmdCtx(author_id=5, guild=_Guild(members=[5], fetch=_fetch))
    stranger = types.SimpleNamespace(id=4242)

    await avatarhistory.AvatarHistory.avatarhistory.callback(cog, ctx, stranger)

    assert cog.rendered == []
    assert ctx.guild.fetched == [4242]


async def test_a_cache_miss_is_confirmed_by_rest_not_read_as_absence():
    """core.py runs with chunk_guilds_at_startup=False, so most members are NOT
    in cache: a get_member miss that refused outright would break the command
    for ordinary members of any large server."""
    async def _fetch(user_id):
        return types.SimpleNamespace(id=user_id)

    cog = _command_cog()
    ctx = _CmdCtx(author_id=5, guild=_Guild(members=[5], fetch=_fetch))
    target = types.SimpleNamespace(id=77)

    await avatarhistory.AvatarHistory.avatarhistory.callback(cog, ctx, target)

    assert ctx.guild.fetched == [77]
    assert cog.rendered == [(77, "global", None)]


async def test_a_cached_member_costs_no_rest_call():
    cog = _command_cog()
    ctx = _CmdCtx(author_id=5, guild=_Guild(members=[5, 77]))
    target = types.SimpleNamespace(id=77)

    await avatarhistory.AvatarHistory.avatarhistory.callback(cog, ctx, target)

    assert ctx.guild.fetched == []
    assert cog.rendered == [(77, "global", None)]


async def test_an_unreachable_api_fails_closed():
    """A network blip must not become a way to read a stranger's history: only
    a positive answer opens the door."""
    async def _fetch(user_id):
        raise _http_error(503)

    cog = _command_cog()
    ctx = _CmdCtx(author_id=5, guild=_Guild(members=[5], fetch=_fetch))
    target = types.SimpleNamespace(id=77)

    await avatarhistory.AvatarHistory.avatarhistory.callback(cog, ctx, target)

    assert cog.rendered == []
    assert "try again" in ctx.sends[0][0]


def _view_with_recorder():
    cog = object.__new__(avatarhistory.AvatarHistory)
    rendered = []

    async def _build_payload(member, kind, guild_id):
        rendered.append(kind)
        return "embed", None

    async def _capture(member):
        return None

    cog.build_payload = _build_payload
    cog.capture_banner = _capture
    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=5), guild=None)
    view = avatarhistory.AvatarHistoryView(cog, ctx, types.SimpleNamespace(id=77))

    class _Message:
        async def edit(self, **kwargs):
            pass

    view.message = _Message()
    return view, rendered


async def test_hammering_the_buttons_does_not_buy_extra_renders():
    """THE BUDGET: every click repaints a collage through the bot-wide Pillow
    semaphore (2 slots for the whole fleet) and the banner tab also runs an
    uncached fetch_user. Only view CREATION was rationed, so one member holding
    a card open could hammer three buttons and monopolise it for free."""
    view, rendered = _view_with_recorder()

    for _i in range(5):
        interaction = _interaction()
        await view._show(interaction, "global")

    assert rendered == ["global"]


async def test_a_throttled_click_is_answered_not_swallowed():
    view, rendered = _view_with_recorder()

    await view._show(_interaction(), "global")
    second = _interaction()
    await view._show(second, "banner")

    assert rendered == ["global"]
    assert second.sent[0][1].get("ephemeral") is True
    assert "too fast" in second.sent[0][0]


async def test_another_member_is_not_throttled_by_someone_elses_click():
    """The window is per user, not global: one member flipping tabs must not
    make the card unusable for the next person who opens their own."""
    view, rendered = _view_with_recorder()

    await view._show(_interaction(user_id=5), "global")
    await view._show(_interaction(user_id=6), "banner")

    assert rendered == ["global", "banner"]


def test_both_doors_into_a_render_are_rationed_at_the_same_rate():
    """The command's cooldown and the buttons' debounce ration the SAME work, so
    the LENGTH is declared once and neither door can be widened alone.

    They are two independent windows, not one shared one (different mechanisms:
    a discord.py bucket keyed on the invoking message, an in-memory debounce
    keyed on the user id), so a member using both doors gets 2 renders per
    window. What this pins is that neither door is cheaper than the other.
    """
    cooldown = avatarhistory.AvatarHistory.avatarhistory._buckets._cooldown
    assert cooldown.per == avatarhistory.HISTORY_COOLDOWN_SECONDS
    assert cooldown.rate == 1
    assert avatarhistory._RENDER_DEBOUNCE.seconds == (
        avatarhistory.HISTORY_COOLDOWN_SECONDS
    )
