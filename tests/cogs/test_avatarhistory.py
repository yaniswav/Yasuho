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


def test_avatar_tracking_is_available_in_user_preferences():
    pref = next(
        item for item in PREFS if item.key == avatarhistory.TRACKING_PREF_KEY
    )
    assert pref.default is True


def test_avatar_series_limit_matches_approved_retention_policy():
    assert avatarhistory.HISTORY_LIMIT == 30
