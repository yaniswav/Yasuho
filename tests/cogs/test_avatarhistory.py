import io
import types

from PIL import Image

from cogs.community import avatarhistory
from cogs.community.usersettings import PREFS


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
    await view._show(types.SimpleNamespace(response=_Response()), "banner")

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
    await view._show(types.SimpleNamespace(response=_Response()), "global")

    assert captured == []
    assert rendered == ["global"]  # the render really ran, so this is not vacuous
