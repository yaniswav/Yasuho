"""Dashboard card background executors (cogs/system/dashboard_card_actions.py)."""

import base64
import inspect
import io
import types

import pytest
from PIL import Image

from cogs.community.leveling import rank_card
from cogs.system import dashboard_actions, dashboard_sync
from cogs.system import dashboard_card_actions as card_actions


class _Engine:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.cleared = []

    async def set_rank_background(self, guild_id, data, content_type=None):
        self.calls.append((guild_id, data, content_type))
        if self.error:
            raise self.error
        return "webp"

    async def clear_rank_card(self, guild_id, *, target=None):
        self.cleared.append((guild_id, target))


def _bot(engine):
    return types.SimpleNamespace(get_cog=lambda name: engine if name == "Leveling" else None)


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


def _b64(data):
    return base64.b64encode(data).decode()


@pytest.fixture(autouse=True)
def _reset_inflight():
    card_actions._INFLIGHT.count = 0
    yield
    card_actions._INFLIGHT.count = 0


def test_executors_are_merged_into_the_queue_table():
    for kind in ("card_background_set", "card_background_clear"):
        assert dashboard_actions._EXECUTORS[kind] is card_actions.EXECUTORS[kind]


async def test_set_success_decodes_and_passes_the_content_type():
    engine = _Engine()
    data = _png()
    result = await card_actions._exec_set(
        _bot(engine), 5, {"image": _b64(data), "content_type": "image/png"}
    )
    assert result == {"ok": True}
    assert engine.calls == [(5, data, "image/png")]
    assert card_actions._INFLIGHT.count == 0


@pytest.mark.parametrize(
    "exc, code",
    [
        (rank_card.SourceTooLarge(), "source_too_large"),
        (rank_card.ImageTooLarge(), "image_too_large"),
        (rank_card.UnsupportedFormat(), "unsupported_format"),
        (rank_card.DecodeFailed(), "decode_failed"),
        (rank_card.EncodedTooLarge(), "encoded_too_large"),
    ],
)
async def test_set_maps_each_typed_error(exc, code):
    result = await card_actions._exec_set(
        _bot(_Engine(error=exc)), 5, {"image": _b64(_png())}
    )
    assert result == {"ok": False, "error": code}
    assert card_actions._INFLIGHT.count == 0  # slot released on a rejection


@pytest.mark.parametrize("payload", [{}, {"image": ""}, {"image": 12}, {"image": "!!notb64"}])
async def test_set_bad_payload(payload):
    engine = _Engine()
    assert await card_actions._exec_set(_bot(engine), 5, payload) == {
        "ok": False,
        "error": "bad_payload",
    }
    assert engine.calls == []
    assert card_actions._INFLIGHT.count == 0


async def test_oversize_base64_is_rejected_before_decoding(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - only on a regression
        raise AssertionError("must not decode an oversize payload")

    monkeypatch.setattr(card_actions.base64, "b64decode", _boom)
    engine = _Engine()
    result = await card_actions._exec_set(
        _bot(engine), 5, {"image": "A" * (card_actions._MAX_B64_CHARS + 1)}
    )
    assert result == {"ok": False, "error": "source_too_large"}
    assert engine.calls == []


async def test_set_without_the_engine():
    assert await card_actions._exec_set(_bot(None), 5, {"image": _b64(_png())}) == {
        "ok": False,
        "error": "engine_unavailable",
    }


async def test_set_busy_when_two_uploads_are_in_flight():
    assert card_actions._INFLIGHT.acquire() and card_actions._INFLIGHT.acquire()
    engine = _Engine()
    result = await card_actions._exec_set(_bot(engine), 5, {"image": _b64(_png())})
    assert result == {"ok": False, "error": "busy"}
    assert engine.calls == []


async def test_clear_and_clear_without_engine():
    engine = _Engine()
    assert await card_actions._exec_clear(_bot(engine), 5, {}) == {"ok": True}
    assert engine.cleared == [(5, "background")]
    assert await card_actions._exec_clear(_bot(None), 5, {}) == {
        "ok": False,
        "error": "engine_unavailable",
    }


async def test_finish_scrubs_the_image_from_the_payload(fake_pool):
    await dashboard_actions._finish(fake_pool, 1, "done", {"ok": True})
    (_, query, _), = fake_pool.calls
    assert "payload = payload - 'image'" in query


def test_every_final_status_write_scrubs_the_image():
    source = inspect.getsource(dashboard_actions)
    finals = [
        chunk
        for chunk in source.split('"UPDATE dashboard_actions "')[1:]
        if "result = $" in chunk.split("WHERE")[0]
    ]
    assert len(finals) == 2  # _finish and the expiry sweep
    assert all("payload - 'image'" in chunk.split("WHERE")[0] for chunk in finals)


async def test_rank_card_sync_also_invalidates_the_guild_settings_cache(monkeypatch):
    seen = []
    monkeypatch.setattr(dashboard_sync.settings, "invalidate_guild", seen.append)
    await dashboard_sync._invalidate_rank_card(types.SimpleNamespace(get_cog=lambda n: None), 9)
    assert seen == [9]
